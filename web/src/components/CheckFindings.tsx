import type { Check, Source } from "@/lib/api";
import { FINDING_KINDS, SOURCE_KINDS } from "@/lib/labels";

function checkedAgainst(sourceId: string | null, sources: Source[]): string | null {
  const source = sources.find((candidate) => candidate.id === sourceId);
  if (!source) return null;
  const kind = SOURCE_KINDS[source.kind ?? ""] ?? "Source";
  return source.label ? `${kind} · ${source.label}` : kind;
}

/**
 * What the independent checker disputes, one card per finding. Every string here was
 * written by a model from text an outsider controls, so it is rendered as text.
 */
export function CheckFindings({ check, sources }: { check: Check; sources: Source[] }) {
  if (check.findings.length === 0) return null;
  return (
    <div className="grid gap-1.5">
      <h2 id="check-findings" className="text-[12.5px] font-semibold text-muted">
        What the checker found
      </h2>
      <ol aria-labelledby="check-findings" className="grid gap-2">
        {check.findings.map((finding, index) => {
          const against = checkedAgainst(finding.source_id, sources);
          return (
            <li key={index} className="grid gap-1.5 rounded-md border border-line bg-surface px-3 py-2.5">
              <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1.5">
                <span className="font-semibold tabular-nums">{index + 1}</span>
                <span className="inline-flex items-center gap-1 rounded bg-review-bg px-1.5 py-0.5 text-xs font-semibold text-review">
                  <span aria-hidden="true">!</span>
                  {FINDING_KINDS[finding.kind]}
                </span>
                {finding.quote ? <span className="font-semibold">“{finding.quote}”</span> : null}
              </div>
              <p>{finding.explanation}</p>
              {against ? <p className="text-[12.5px] text-muted">Checked against: {against}</p> : null}
            </li>
          );
        })}
      </ol>
      <p className="text-[12.5px] text-muted">
        {check.on_current_answer
          ? "A second model read the question, this draft and its sources, and nothing else."
          : "The checker read the agent's draft, not the wording shown now."}
      </p>
    </div>
  );
}
