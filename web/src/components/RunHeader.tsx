import type { Run } from "@/lib/api";
import { money } from "@/lib/labels";

import { StatusChip } from "./StatusChip";

type Props = { run: Run; filename: string; live: boolean; needs: number; ready: number };

const OPEN = ["queued", "running"];

export function RunHeader({ run, filename, live, needs, ready }: Props) {
  const total = Object.values(run.counts).reduce((sum, count) => sum + count, 0);
  const open = OPEN.reduce((sum, status) => sum + (run.counts[status] ?? 0), 0);
  const answered = total - open;
  const percent = total === 0 ? 100 : Math.round((answered / total) * 100);
  return (
    <header className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-line bg-surface px-4 py-2.5">
      <span className="font-semibold">Standingproof</span>
      <span className="min-w-0 truncate text-muted">{filename}</span>
      <div className="flex w-full flex-wrap items-center gap-x-3.5 gap-y-2 tabular-nums md:ml-auto md:w-auto">
        <span className="inline-flex items-center gap-1.5 text-muted" role="status">
          <span
            aria-hidden="true"
            className={`size-2 rounded-full ${live ? "bg-ready" : "bg-review"}`}
          />
          {live ? "Live" : "Not live: checking every 5 seconds"}
        </span>
        <span
          role="progressbar"
          aria-label="Questions answered"
          aria-valuemin={0}
          aria-valuemax={total}
          aria-valuenow={answered}
          className="h-1.5 min-w-30 flex-1 overflow-hidden rounded-full bg-sunk md:w-35 md:flex-none"
        >
          <span className="block h-full bg-accent" style={{ width: `${percent}%` }} />
        </span>
        <span>
          {answered} of {total} answered
        </span>
        {needs > 0 ? <StatusChip tone="review" mark="!" label={`${needs} need${needs === 1 ? "s" : ""} review`} /> : null}
        {ready > 0 ? <StatusChip tone="ready" mark="✓" label={`${ready} ready`} /> : null}
        <span className="text-[12.5px] text-muted">
          {money(run.cost_usd)} of {money(run.max_cost_usd)}
        </span>
      </div>
    </header>
  );
}
