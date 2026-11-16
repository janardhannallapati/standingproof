"use client";

import type { Decision, ReviewQuestion } from "@/lib/api";
import { chipFor, fineMoney, groupOf, reasonText } from "@/lib/labels";

import { DecisionBar, type Mode } from "./DecisionBar";
import { SourceCard } from "./SourceCard";
import { StatusChip } from "./StatusChip";

type Props = {
  question: ReviewQuestion;
  mode: Mode;
  onMode: (mode: Mode) => void;
  onDecide: (decision: Decision, extra?: { answer?: string; comment?: string }) => void;
  busy: boolean;
  // Something the reviewer must know about their last action (a refusal, a warning).
  notice: string | null;
  maxRedrafts: number;
};

function why(question: ReviewQuestion): { fine: boolean; text: string } {
  const group = groupOf(question);
  if (group === "needs" || group === "unanswered") {
    const note = question.note ? ` ${question.note}` : "";
    return { fine: false, text: `${reasonText(question.reason)}.${note}` };
  }
  if (question.path === "reused") {
    return { fine: true, text: "Reused: the question matches an approved answer exactly." };
  }
  const count = question.sources.length;
  return {
    fine: true,
    text: `Drafted from ${count} cited source${count === 1 ? "" : "s"}.${question.note ? ` ${question.note}` : ""}`,
  };
}

function meta(question: ReviewQuestion): string {
  const parts = [question.path ?? "not drafted"];
  if (question.steps > 0) parts.push(`${question.steps} step${question.steps === 1 ? "" : "s"}`);
  parts.push(question.steps === 0 && question.path === "reused" ? "no model call" : fineMoney(question.cost_usd));
  if (question.redrafts > 0) parts.push(`redrafted ${question.redrafts}×`);
  return parts.join(" · ");
}

export function QuestionDetail({ question, mode, onMode, onDecide, busy, notice, maxRedrafts }: Props) {
  const reason = why(question);
  const pending = question.review === "pending";
  const working = groupOf(question) === "working";
  const shown = question.final_answer ?? question.answer;
  return (
    <section aria-label={`Question ${question.position}`} className="grid min-w-0 content-start gap-4 p-4 md:px-6 md:py-5">
      <h1 className="text-[17px] leading-snug font-semibold">{question.question}</h1>

      <p
        className={`flex items-start gap-2.5 rounded-md px-3 py-2.5 font-medium ${
          reason.fine ? "bg-ready-bg text-ready" : "bg-review-bg text-review"
        }`}
      >
        <span aria-hidden="true">{reason.fine ? "✓" : "!"}</span>
        <span>{reason.text}</span>
      </p>

      {notice ? (
        <p role="alert" className="rounded-md bg-review-bg px-3 py-2.5 font-medium text-review">
          {notice}
        </p>
      ) : null}

      {mode !== "edit" ? (
        <div className="grid gap-1.5">
          <h2 className="text-[12.5px] font-semibold text-muted">
            {question.final_answer ? "Approved answer" : "Proposed answer"}
          </h2>
          <p
            className={`min-h-16 rounded-md border border-line bg-surface px-3.5 py-3 text-[15px] leading-normal whitespace-pre-wrap ${
              shown ? "" : "text-muted"
            }`}
          >
            {shown || "No answer was drafted. Write one, or reject the question back to its owner."}
          </p>
          {question.final_answer && question.answer && question.final_answer !== question.answer ? (
            <p className="text-[12.5px] text-muted">The agent&apos;s draft was: {question.answer}</p>
          ) : null}
        </div>
      ) : null}

      <div className="grid gap-1.5">
        <h2 className="text-[12.5px] font-semibold text-muted">
          {question.sources.length === 1 ? "Source" : "Sources"}
        </h2>
        {question.sources.length === 0 ? (
          <p className="rounded-md border border-line bg-surface px-3 py-2.5 text-[12.5px] text-muted">
            No source was found, so there is nothing to cite.
          </p>
        ) : (
          question.sources.map((source) => (
            <SourceCard key={source.id} source={source} answer={shown} />
          ))
        )}
      </div>

      <div className="sticky bottom-0 grid gap-2 border-t border-line bg-bg py-2.5 md:static md:border-0 md:bg-transparent md:py-0">
        {pending ? (
          <DecisionBar
            question={question}
            mode={mode}
            onMode={onMode}
            onDecide={onDecide}
            busy={busy}
            maxRedrafts={maxRedrafts}
          />
        ) : (
          <p className="flex items-center gap-2">
            <StatusChip {...chipFor(question)} />
            <span className="text-muted">
              {working ? "The agent is working on this question." : "A decision has been recorded."}
            </span>
          </p>
        )}
        <p className="text-[12.5px] text-muted tabular-nums">{meta(question)}</p>
      </div>
    </section>
  );
}
