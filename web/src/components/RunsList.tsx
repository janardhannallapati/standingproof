import Link from "next/link";

import type { RunListItem } from "@/lib/api";

import { StatusChip } from "./StatusChip";

function when(value: string): string {
  return new Date(value).toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
}

export function RunsList({ runs }: { runs: RunListItem[] }) {
  return (
    <ul className="overflow-hidden rounded-md border border-line bg-surface">
      {runs.map((run) => (
        <li key={run.id} className="border-b border-line last:border-0">
          <Link href={`/runs/${run.id}`} className="flex min-h-11 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3">
            <span className="font-medium">{run.filename}</span>
            <span className="text-muted">{run.status}</span>
            {run.reopened > 0 ? (
              // An answer in this run rests on a live setting that has changed since.
              <StatusChip
                tone="review"
                mark="!"
                label={`${run.reopened} ${run.reopened === 1 ? "answer" : "answers"} reopened`}
              />
            ) : null}
            <span className="text-[12.5px] text-muted tabular-nums md:ml-auto">{when(run.created_at)}</span>
          </Link>
        </li>
      ))}
    </ul>
  );
}
