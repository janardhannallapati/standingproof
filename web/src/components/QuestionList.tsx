import type { RunQuestion } from "@/lib/api";
import { GROUP_ORDER, GROUP_TITLES, type Group, chipFor, fineMoney, groupOf, isReopened, reasonText } from "@/lib/labels";

import { StatusChip } from "./StatusChip";

type Props = {
  questions: RunQuestion[];
  selectedId: string | null;
  onSelect: (id: string) => void;
  // Shown beside the "Ready" heading. Asking twice is the caller's job.
  readyAction?: React.ReactNode;
};

function subline(question: RunQuestion): string {
  const group = groupOf(question);
  // Why it is back, not why the agent's draft once needed review.
  if (group === "needs" && isReopened(question)) return "a live setting changed";
  if (group === "needs" || group === "unanswered") return reasonText(question.reason);
  if (question.path === "reused") return "reused";
  if (question.path === "drafted") return `drafted · ${fineMoney(question.cost_usd)}`;
  return "";
}

export function QuestionList({ questions, selectedId, onSelect, readyAction }: Props) {
  const groups = new Map<Group, RunQuestion[]>();
  for (const question of questions) {
    const group = groupOf(question);
    groups.set(group, [...(groups.get(group) ?? []), question]);
  }
  return (
    <nav aria-label="Questions" className="bg-surface">
      {GROUP_ORDER.filter((group) => groups.has(group)).map((group) => {
        const members = groups.get(group) ?? [];
        return (
          <section key={group} aria-label={`${GROUP_TITLES[group]}, ${members.length}`}>
            <div className="flex min-h-11 items-center justify-between gap-2.5 border-b border-line bg-sunk px-3.5 py-1.5 text-[13px] font-semibold">
              <h2>
                {GROUP_TITLES[group]} ({members.length})
              </h2>
              {group === "ready" ? readyAction : null}
            </div>
            <ul>
              {members.map((question) => {
                const current = question.id === selectedId;
                return (
                  <li key={question.id}>
                    <button
                      type="button"
                      onClick={() => onSelect(question.id)}
                      aria-current={current ? "true" : undefined}
                      className={`grid min-h-15 w-full grid-cols-[22px_1fr] gap-x-2 gap-y-1 border-b border-line px-3.5 py-2.5 text-left ${
                        current ? "bg-select shadow-[inset_3px_0_0_var(--s-accent)]" : ""
                      }`}
                    >
                      <span className="text-muted tabular-nums">{question.position}</span>
                      <span className="line-clamp-2 font-medium">{question.question}</span>
                      <span className="col-start-2 flex flex-wrap items-center gap-2">
                        <StatusChip {...chipFor(question)} />
                        <span className="text-[12.5px] text-muted tabular-nums">{subline(question)}</span>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </nav>
  );
}
