import type { Tone } from "@/lib/labels";

const TONES: Record<Tone, string> = {
  ready: "bg-ready-bg text-ready",
  review: "bg-review-bg text-review",
  fail: "bg-fail-bg text-fail",
  quiet: "bg-sunk text-muted",
};

// State is always a mark and a word as well as a colour.
export function StatusChip({ tone, mark, label }: { tone: Tone; mark: string; label: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-xs font-semibold whitespace-nowrap ${TONES[tone]}`}
    >
      <span aria-hidden="true">{mark}</span>
      {label}
    </span>
  );
}
