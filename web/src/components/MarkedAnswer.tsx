import type { Finding } from "@/lib/api";

type Mark = { start: number; end: number; number: number };

/**
 * Where each located finding's words stand in the answer. A finding is numbered by its
 * place in the list, so the number here matches the number on its card. Words already
 * marked by an earlier finding are not marked again.
 */
export function marksFor(answer: string, findings: Finding[]): Mark[] {
  const marks: Mark[] = [];
  findings.forEach((finding, index) => {
    if (!finding.located || !finding.quote) return;
    const start = answer.indexOf(finding.quote);
    if (start < 0) return;
    const end = start + finding.quote.length;
    if (marks.some((mark) => start < mark.end && mark.start < end)) return;
    marks.push({ start, end, number: index + 1 });
  });
  return marks.sort((a, b) => a.start - b.start);
}

/** The answer as text, with the words the checker disputes marked and numbered. */
export function MarkedAnswer({ answer, findings }: { answer: string; findings: Finding[] }) {
  const parts: React.ReactNode[] = [];
  let at = 0;
  for (const mark of marksFor(answer, findings)) {
    parts.push(answer.slice(at, mark.start));
    parts.push(
      // A tint, an underline and a number: the mark does not depend on colour.
      <mark
        key={mark.number}
        data-finding={mark.number}
        className="rounded-sm border-b-2 border-review bg-review-bg px-0.5 text-inherit"
      >
        <span className="sr-only"> (flagged, finding {mark.number}: </span>
        {answer.slice(mark.start, mark.end)}
        <span className="sr-only">) </span>
        <sup aria-hidden="true" className="ml-0.5 font-semibold text-review">
          {mark.number}
        </sup>
      </mark>,
    );
    at = mark.end;
  }
  parts.push(answer.slice(at));
  return <>{parts}</>;
}
