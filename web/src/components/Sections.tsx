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
            className={`inline-flex min-h-11 items-center rounded-md px-3 text-xl font-semibold md:min-h-9 ${
              here ? "text-fg" : "text-muted underline"
            }`}
          >
            {section.label}
          </Link>
        );
      })}
    </nav>
  );
}
