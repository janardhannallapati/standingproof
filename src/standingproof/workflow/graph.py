"""The graph for one question: which step follows which.

    exact_match ──(matched)──────────────────────────► END
         │
         └─► call_model ──(finished or stopped)──────► END
                 ▲   │
                 │   └─(asked for tools)─► run_tools
                 └────────────────────────────┘

State is saved after every step under the question's thread id. Running a thread that
already has saved state continues it; running a finished one returns its outcome.
"""

from typing import Any, Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from standingproof.agent.types import AnswerDraft, Usage
from standingproof.workflow.nodes import call_model, exact_match, run_tools
from standingproof.workflow.state import (
    Outcome,
    QuestionContext,
    QuestionState,
    initial_state,
)

QuestionGraph = CompiledStateGraph[QuestionState, QuestionContext, QuestionState, QuestionState]


def _after_exact_match(state: QuestionState) -> Literal["call_model", "__end__"]:
    return "__end__" if state["outcome"] is not None else "call_model"


def _after_model(state: QuestionState) -> Literal["run_tools", "__end__"]:
    return "__end__" if state["outcome"] is not None else "run_tools"


def build_graph(checkpointer: BaseCheckpointSaver[Any]) -> QuestionGraph:
    builder = StateGraph(QuestionState, context_schema=QuestionContext)
    builder.add_node("exact_match", exact_match)
    builder.add_node("call_model", call_model)
    builder.add_node("run_tools", run_tools)
    builder.add_edge(START, "exact_match")
    builder.add_conditional_edges("exact_match", _after_exact_match)
    builder.add_conditional_edges("call_model", _after_model)
    builder.add_edge("run_tools", "call_model")
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
    graph: QuestionGraph, *, thread_id: str, question: str, context: QuestionContext
) -> Outcome:
    """Run one question to its end, from wherever its saved state says it is."""
    config: Any = {
        "configurable": {"thread_id": thread_id},
        # The limits in the context end a run long before this; it only bounds a bug.
        "recursion_limit": 4 * context.limits.max_steps + 10,
    }
    snapshot = await graph.aget_state(config)
    if snapshot.values and snapshot.values.get("outcome") is not None:
        return _outcome(snapshot.values)
    # None means "continue from the saved state"; a new thread starts from the question.
    start = None if snapshot.values else initial_state(question)
    # sync: a step's result is in the database before the next step starts.
    final = await graph.ainvoke(start, config, context=context, durability="sync")
    return _outcome(final)
