import type { ButtonHTMLAttributes, ReactNode } from "react";

type Props = ButtonHTMLAttributes<HTMLButtonElement> & {
  primary?: boolean;
  // The single key that does the same thing, shown on the button.
  shortcut?: string;
  children: ReactNode;
};

export function Button({ primary, shortcut, children, className = "", ...rest }: Props) {
  const look = primary
    ? "bg-accent text-on-accent border-accent font-semibold"
    : "bg-surface text-fg border-line font-medium";
  return (
    <button
      type="button"
      {...rest}
      className={`inline-flex min-h-11 items-center justify-center gap-2 rounded-md border px-3.5 disabled:opacity-50 md:min-h-9 ${look} ${className}`}
    >
      {children}
      {shortcut ? (
        <kbd
          aria-hidden="true"
          className="hidden rounded-sm border border-current px-1 py-0.5 font-sans text-[11px] leading-none font-semibold opacity-75 md:inline"
        >
          {shortcut}
        </kbd>
      ) : null}
    </button>
  );
}
