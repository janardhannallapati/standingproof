import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FilePreview } from "@/components/FilePreview";
import { RunHeader } from "@/components/RunHeader";
import type { FilePreview as Preview, Run } from "@/lib/api";
import { allDecided, emptyReason } from "@/lib/labels";

import { listed } from "./fixtures";

function preview(overrides: Partial<Preview> = {}): Preview {
  return {
    filename: "acme-filled.xlsx",
    ready: true,
    undecided: 0,
    written: [
      { position: 1, sheet: "Security", cell: "C2", question: "Is MFA enforced?", wording: "Yes, for all staff.", how: "approved" },
      { position: 2, sheet: "Security", cell: "C3", question: "Is data encrypted?", wording: "Yes. AES-256.", how: "edited" },
    ],
    left_empty: [
      { position: 3, sheet: "Security", cell: "C4", question: "Are backups tested?", reason: "rejected", detail: "", comment: "Ask the backup owner." },
    ],
    protected_sheets: [],
    latest: null,
    ...overrides,
  };
}

afterEach(() => vi.unstubAllGlobals());

describe("FilePreview", () => {
  it("says what will be written and what is left empty, in one line", () => {
    render(<FilePreview runId="r1" preview={preview()} />);

    expect(screen.getByText(/^2 answers will be written\. 1 cell is left empty\./)).toBeInTheDocument();
  });

  it("lists each written cell with its question, wording and how it was approved", () => {
    render(<FilePreview runId="r1" preview={preview()} />);

    const table = screen.getByRole("table", { name: "What is written" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Cell", "Was", "Will be"]);
    const [, first, second] = within(table).getAllByRole("row");
    expect(first).toHaveTextContent("Security!C2");
    expect(first).toHaveTextContent("empty");
    expect(first).toHaveTextContent("Is MFA enforced?");
    expect(first).toHaveTextContent("Yes, for all staff.");
    expect(first).toHaveTextContent("approved");
    expect(second).toHaveTextContent("edited by a reviewer");
  });

  it("lists each cell left empty with the reason in words and the reviewer's note", () => {
    render(<FilePreview runId="r1" preview={preview()} />);

    const item = within(screen.getByRole("list", { name: "Left empty" })).getByRole("listitem");
    expect(item).toHaveTextContent("Security!C4");
    expect(item).toHaveTextContent("Rejected");
    expect(item).toHaveTextContent("Ask the backup owner.");
  });

  it("offers no download while a question is undecided, and says how many", () => {
    render(<FilePreview runId="r1" preview={preview({ ready: false, undecided: 2 })} />);

    expect(screen.getByText("2 questions still need a decision.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Download filled file" })).toBeNull();
    expect(screen.getByRole("link", { name: "Back to the questions" })).toHaveAttribute("href", "/runs/r1");
  });

  it("makes the file, then sends the browser to it", async () => {
    const calls: string[] = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      calls.push(`${init?.method ?? "GET"} ${url}`);
      return Response.json({ id: "f1", sha256: "ab", size_bytes: 6000, created_at: "2026-11-16T10:00:00Z", created_by: "asha", current: true }, { status: 201 });
    });
    const go = vi.fn();
    render(<FilePreview runId="r1" preview={preview()} navigate={go} />);

    await userEvent.click(screen.getByRole("button", { name: "Download filled file" }));

    expect(calls).toEqual(["POST /api/runs/r1/file"]);
    expect(go).toHaveBeenCalledWith("/api/runs/r1/file/f1");
  });

  it("shows why when the file could not be made, and downloads nothing", async () => {
    vi.stubGlobal("fetch", async () =>
      Response.json({ detail: "The file was not made: xl/worksheets/sheet1.xml: another cell in row 2 changed" }, { status: 422 }),
    );
    const go = vi.fn();
    render(<FilePreview runId="r1" preview={preview()} navigate={go} />);

    await userEvent.click(screen.getByRole("button", { name: "Download filled file" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("another cell in row 2 changed");
    expect(go).not.toHaveBeenCalled();
  });

  it("says when an earlier file was made before a decision changed", () => {
    const latest = { id: "f0", sha256: "cd", size_bytes: 5900, created_at: "2026-11-16T09:00:00Z", created_by: "asha", current: false };
    render(<FilePreview runId="r1" preview={preview({ latest })} />);

    expect(screen.getByText(/A file was made earlier, before a decision changed/)).toBeInTheDocument();
  });

  it("says when a sheet is protected", () => {
    render(<FilePreview runId="r1" preview={preview({ protected_sheets: ["Security"] })} />);

    expect(screen.getByText(/The sheet Security is protected/)).toBeInTheDocument();
  });

  it("gives a reason in words for every kind of empty cell", () => {
    for (const reason of ["rejected", "expired", "unanswered", "no_answer", "no_answer_cell", "sheet_missing", "cell_not_empty", "dropdown", "too_long", "bad_character", "undecided"]) {
      expect(emptyReason(reason, "Yes, No").text).not.toContain("_");
    }
    expect(emptyReason("dropdown", "Yes, No").text).toContain("Yes, No");
  });
});

describe("the way to the filled file", () => {
  const run = { id: "r1", status: "finished", counts: { ready: 2 }, cost_usd: "0.07", max_cost_usd: "5.0000" } as unknown as Run;

  it("is in the header once every question has a decision", () => {
    render(<RunHeader run={run} filename="acme.xlsx" live needs={0} ready={0} fileHref="/runs/r1/file" />);

    expect(screen.getByRole("link", { name: "Filled file" })).toHaveAttribute("href", "/runs/r1/file");
  });

  it("is not there before that", () => {
    render(<RunHeader run={run} filename="acme.xlsx" live needs={1} ready={0} fileHref={null} />);

    expect(screen.queryByRole("link", { name: "Filled file" })).toBeNull();
  });

  it("counts a question as decided only when nobody still has to act on it", () => {
    const approved = listed({ review: "approved" });
    expect(allDecided([approved, listed({ review: "rejected", status: "needs_review" })])).toBe(true);
    expect(allDecided([approved, listed({ status: "failed", review: null })])).toBe(true);
    expect(allDecided([approved, listed({ review: "pending" })])).toBe(false);
    expect(allDecided([approved, listed({ review: "redrafting" })])).toBe(false);
    expect(allDecided([approved, listed({ status: "running", review: null })])).toBe(false);
    expect(allDecided([])).toBe(false);
  });
});
