import Link from "next/link";

const SECTIONS = [
  { href: "/runs", label: "Runs" },
  { href: "/changes", label: "Changes" },
] as const;

/** The two top-level pages, and which one this is. */
export function Sections({ current }: { current: (typeof SECTIONS)[number]["href"] }) {
  return (
    <nav aria-label="Sections" className="flex flex-wrap items-center gap-x-1 gap-y-1">
      {SECTIONS.map((section) => {
        const here = section.href === current;
        return (
          <Link
            key={section.href}
            href={section.href}
            aria-current={here ? "page" : undefined}
            // The page you are on: a bar under it and no underline, as well as the colour.
            className={`inline-flex min-h-11 items-center border-b-2 px-3 text-xl font-semibold md:min-h-9 ${
              here ? "border-accent text-fg" : "border-transparent text-muted underline"
            }`}
          >
            {section.label}
          </Link>
        );
      })}
    </nav>
  );
}
