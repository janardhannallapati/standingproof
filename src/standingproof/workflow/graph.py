"""The graph for one question: which step follows which.

    exact_match ──(matched)──────────────────────────► review ──(approve, edit, reject)─► END
         │                                               ▲  │
         └─► call_model ──(finished or stopped)──────────┘  │
                 ▲   │  ▲                                    │
                 │   │  └──────────(redraft)─────────────────┘
                 │   └─(asked for tools)─► run_tools
                 └────────────────────────────┘

State is saved after every step under the question's thread id. At `review` the graph
stops and waits for a person: no process holds it, it is rows in the database. Running a
thread that has saved state continues it; one that is waiting or ended returns its outcome.
"""

from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command

from standingproof.agent.types import AnswerDraft, Usage
from standingproof.workflow.nodes import call_model, exact_match, review, run_tools
from standingproof.workflow.state import (
    Outcome,
    Progress,
    QuestionContext,
    QuestionState,
    initial_state,
)

QuestionGraph = CompiledStateGraph[QuestionState, QuestionContext, QuestionState, QuestionState]


def _after_exact_match(state: QuestionState) -> Literal["call_model", "review"]:
    return "review" if state["outcome"] is not None else "call_model"


def _after_model(state: QuestionState) -> Literal["run_tools", "review"]:
    return "review" if state["outcome"] is not None else "run_tools"


def _after_review(state: QuestionState) -> Literal["call_model", "__end__"]:
    # A redraft cleared the outcome; any other decision left it and ends the question.
    return "__end__" if state["outcome"] is not None else "call_model"


def build_graph(checkpointer: BaseCheckpointSaver[Any]) -> QuestionGraph:
    builder = StateGraph(QuestionState, context_schema=QuestionContext)
    builder.add_node("exact_match", exact_match)
    builder.add_node("call_model", call_model)
    builder.add_node("run_tools", run_tools)
    builder.add_node("review", review)
    builder.add_edge(START, "exact_match")
    builder.add_conditional_edges("exact_match", _after_exact_match)
    builder.add_conditional_edges("call_model", _after_model)
    builder.add_edge("run_tools", "call_model")
    builder.add_conditional_edges("review", _after_review)
    return builder.compile(checkpointer=checkpointer)


def _outcome(state: dict[str, Any]) -> Outcome:
    saved = state["outcome"]
    return Outcome(
        status=saved["status"],
        path=saved["path"],
        reason=saved["reason"],
        draft=AnswerDraft(**saved["draft"]) if saved["draft"] else None,
        steps=state["steps"],
        cost_usd=state["cost_usd"],
        usage=Usage(**state["usage"]),
    )


async def run_question(
    graph: QuestionGraph,
    *,
    thread_id: str,
    question: str,
    context: QuestionContext,
    resume: dict[str, str] | None = None,
) -> Outcome:
    """Run one question until it stops for review, from wherever its saved state says it is.

    `resume` is a reviewer's decision. It is used only if the question is waiting for one;
    passed again after the graph has already taken it, it is ignored.
    """
    config: Any = {
        "configurable": {"thread_id": thread_id},
        # The limits in the context end a run long before this; it only bounds a bug.
        "recursion_limit": 4 * context.limits.max_steps + 12,
    }
    snapshot = await graph.aget_state(config)
    start: Any
    if not snapshot.values:
        start = initial_state(question)
    elif snapshot.interrupts:
        if resume is None:
            return _outcome(snapshot.values)  # still waiting for a person
        start = Command(resume=resume)
    elif not snapshot.next:
        return _outcome(snapshot.values)  # the question has ended
    else:
        start = None  # stopped part way: continue from the saved state
    # sync: a step's result is in the database before the next step starts.
    final = await graph.ainvoke(start, config, context=context, durability="sync")
    return _outcome(final)


async def progress(graph: QuestionGraph, thread_id: str) -> Progress | None:
    """What the saved state says a question has used. None if nothing was saved.

    A model call whose step never reached a checkpoint (the process died during it) is not
    in here: it was paid for and there is no record of how much.
    """
    snapshot = await graph.aget_state({"configurable": {"thread_id": thread_id}})
    if not snapshot.values:
        return None
    values = snapshot.values
    return Progress(
        steps=values["steps"], cost_usd=values["cost_usd"], usage=Usage(**values["usage"])
    )
