import type { Source } from "@/lib/api";

const KINDS: Record<string, string> = {
  answer: "Approved answer",
  policy: "Policy",
  evidence: "Live check",
};

/**
 * The longest sentence of the answer that the source also contains, as a range in the
 * source. That is the part the answer relies on, and it is what gets highlighted.
 */
export function citedRange(sourceText: string, answer: string | null): [number, number] | null {
  if (!answer) return null;
  const whole = sourceText.indexOf(answer.trim());
  if (answer.trim() && whole >= 0) return [whole, whole + answer.trim().length];
  const sentences = answer
    .split(/(?<=[.!?])\s+/)
    .map((part) => part.trim())
    .filter((part) => part.length >= 12)
    .sort((a, b) => b.length - a.length);
  for (const sentence of sentences) {
    const at = sourceText.indexOf(sentence);
    if (at >= 0) return [at, at + sentence.length];
  }
  return null;
}

function day(value: string): string {
  return new Date(value).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

export function SourceCard({ source, answer }: { source: Source; answer: string | null }) {
  if (!source.available || source.text == null) {
    return (
      <div className="rounded-md border border-line bg-surface px-3 py-2.5 text-muted">
        This source can no longer be found. It may have been removed from the library since the
        draft was written.
      </div>
    );
  }
  const range = citedRange(source.text, answer);
  return (
    <article className="rounded-md border border-line bg-surface">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b border-line px-3 py-2 text-[12.5px] text-muted">
        <b className="font-semibold text-fg">{KINDS[source.kind ?? ""] ?? "Source"}</b>
        {source.owner ? <span>owner: {source.owner}</span> : null}
        {source.valid_until ? <span>valid until {day(source.valid_until)}</span> : null}
        {source.observed_at ? <span>observed {day(source.observed_at)}</span> : null}
      </header>
      {source.label ? <p className="px-3 pt-2.5 text-muted">{source.label}</p> : null}
      <p className="px-3 py-2.5">
        {range ? (
          <>
            {source.text.slice(0, range[0])}
            <mark className="rounded-sm bg-hl px-0.5 text-inherit">{source.text.slice(range[0], range[1])}</mark>
            {source.text.slice(range[1])}
          </>
        ) : (
          source.text
        )}
      </p>
    </article>
  );
}
