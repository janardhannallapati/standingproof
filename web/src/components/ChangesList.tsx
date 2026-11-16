import Link from "next/link";

import type { Change } from "@/lib/api";
import { factLabel, factValue, sentList, utcDay } from "@/lib/labels";

import { StatusChip } from "./StatusChip";

function seen(iso: string): string {
  const time = new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  return `seen ${utcDay(iso)}, ${time} UTC`;
}

function chip(change: Change) {
  const count = change.answers.length;
  if (count === 0) return <StatusChip tone="quiet" mark="–" label="No answer rests on it" />;
  const waiting = change.answers.some((answer) => answer.waiting);
  const label = `${count} ${count === 1 ? "answer" : "answers"} reopened`;
  // Decided again: it is a record now, not something waiting for anyone.
  return waiting ? (
    <StatusChip tone="review" mark="!" label={label} />
  ) : (
    <StatusChip tone="quiet" mark="✓" label={`${label}, since decided`} />
  );
}

/**
 * Each change seen to a live setting, with the answers it put back in front of a
 * reviewer. Nothing is decided here: every answer links to its own question.
 */
export function ChangesList({ changes }: { changes: Change[] }) {
  if (changes.length === 0) {
    return (
      <p className="text-muted">
        No live setting has been seen to change since an answer rested on it. Settings are
        looked at once a day, and at once when GitHub reports a change.
      </p>
    );
  }
  return (
    <ul className="grid gap-2.5">
      {changes.map((change) => (
        <li key={change.id} className="grid gap-2 rounded-md border border-line bg-surface px-4 py-3">
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
            {chip(change)}
            <h2 className="min-w-0 font-semibold break-words">
              {change.check_name} on {change.scope}
            </h2>
            <span className="text-[12.5px] text-muted tabular-nums md:ml-auto">{seen(change.now.observed_at)}</span>
          </div>
          <p>{change.now.statement}</p>
          {change.changes.length > 0 ? (
            // Lines, not a nested list: the page is one list of changes.
            <div className="grid gap-0.5 text-[12.5px] text-muted">
              {change.changes.map((changed) => (
                <p key={changed.fact}>
                  {factLabel(changed.fact)}: was {factValue(changed.was)}, now{" "}
                  <b className="font-semibold text-fg">{factValue(changed.now)}</b>
                </p>
              ))}
            </div>
          ) : null}
          {change.answers.map((answer) => (
            <div key={answer.run_question_id} className="grid gap-0.5 border-t border-line pt-2">
              <Link
                href={`/runs/${answer.run_id}?question=${answer.run_question_id}`}
                className="inline-flex min-h-11 items-center font-medium text-accent underline md:min-h-9"
              >
                {answer.filename} · question {answer.position}: {answer.question}
              </Link>
              <span className="text-[12.5px] text-muted">
                {answer.sent_to.length > 0 ? `Sent ${sentList(answer.sent_to)}` : "Not recorded as sent to anyone"}
              </span>
            </div>
          ))}
        </li>
      ))}
    </ul>
  );
}
