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
      { position: 1, sheet: "Security", cell: "C2", question: "Is MFA enforced?", wording: "Yes, for all staff.", how: "approved", kind: "answer" },
      { position: 2, sheet: "Security", cell: "C3", question: "Is data encrypted?", wording: "Yes. AES-256.", how: "edited", kind: "answer" },
    ],
    left_empty: [
      { position: 3, sheet: "Security", cell: "C4", question: "Are backups tested?", reason: "rejected", detail: "", comment: "Ask the backup owner." },
    ],
    protected_sheets: [],
    latest: null,
    sent: [],
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

  it("keeps the keyboard's place on the button when the file could not be made", async () => {
    vi.stubGlobal("fetch", async () => Response.json({ detail: "Not made." }, { status: 422 }));
    render(<FilePreview runId="r1" preview={preview()} navigate={vi.fn()} />);
    const button = screen.getByRole("button", { name: "Download filled file" });

    await userEvent.click(button);

    await screen.findByRole("alert");
    expect(screen.getByRole("button", { name: "Download filled file" })).toHaveFocus();
  });

  it("does not ask twice while the file is being made", async () => {
    let finish: (response: Response) => void = () => {};
    const calls: string[] = [];
    vi.stubGlobal("fetch", (url: string) => {
      calls.push(url);
      return new Promise<Response>((resolve) => (finish = resolve));
    });
    render(<FilePreview runId="r1" preview={preview()} navigate={vi.fn()} />);

    await userEvent.click(screen.getByRole("button", { name: "Download filled file" }));
    const busy = screen.getByRole("button", { name: "Making the file…" });
    expect(busy).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(busy);

    expect(calls).toHaveLength(1);
    finish(Response.json({ id: "f1", sha256: "ab", size_bytes: 1, created_at: "2026-11-16T10:00:00Z", created_by: "a", current: true }, { status: 201 }));
    await screen.findByRole("button", { name: "Download filled file" });
  });

  it("lets a keyboard reach the table's box, which scrolls sideways on a phone", () => {
    render(<FilePreview runId="r1" preview={preview()} />);

    const box = screen.getByRole("region", { name: "What is written (2)" });
    expect(box).toHaveAttribute("tabindex", "0");
  });

  it("makes the way back a target big enough for a thumb", () => {
    render(<FilePreview runId="r1" preview={preview({ ready: false, undecided: 1 })} />);

    expect(screen.getByRole("link", { name: "Back to the questions" })).toHaveClass("min-h-11");
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

describe("a dropdown with a comments column", () => {
  const written = [
    { position: 1, sheet: "Security", cell: "C2", question: "Is MFA enforced?", wording: "Yes", how: "approved", kind: "choice" },
    { position: 1, sheet: "Security", cell: "D2", question: "Is MFA enforced?", wording: "Yes. For all staff.", how: "approved", kind: "comment" },
  ];

  it("counts answers and cells apart, and says where each cell's text came from", () => {
    render(<FilePreview runId="r1" preview={preview({ written, left_empty: [] })} />);

    expect(screen.getByText(/^1 answer will be written, in 2 cells\. 0 cells are left empty\./)).toBeInTheDocument();
    const [, choice, comment] = within(screen.getByRole("table", { name: "What is written" })).getAllByRole("row");
    expect(choice).toHaveTextContent("Security!C2");
    expect(choice).toHaveTextContent("the dropdown's value, taken from the first word of the approved answer");
    expect(comment).toHaveTextContent("Security!D2");
    expect(comment).toHaveTextContent("approved · the full answer, in the comments column");
  });

  it("says where the answer is when a dropdown is left for the reviewer to choose", () => {
    const comment = written[1]!;
    const dropdown = { position: 1, sheet: "Security", cell: "C2", question: "Is MFA enforced?", reason: "dropdown", detail: "Yes, No, N/A", comment: null };
    render(<FilePreview runId="r1" preview={preview({ written: [comment], left_empty: [dropdown] })} />);

    const item = within(screen.getByRole("list", { name: "Left empty" })).getByRole("listitem");
    expect(item).toHaveTextContent("The full answer is in the comments column beside it. Choose the value in the spreadsheet.");
  });

  it("warns, with no tick, when no answer would be written", () => {
    render(<FilePreview runId="r1" preview={preview({ written: [] })} />);

    const line = screen.getByText(/^No answer will be written\./).closest("p");
    expect(line).toHaveClass("text-review");
    expect(line).toHaveTextContent(/^!/);
  });
});

describe("recording a file as sent", () => {
  const latest = { id: "f1", sha256: "ab", size_bytes: 6000, created_at: "2026-11-16T10:00:00Z", created_by: "asha", current: true };
  const record = { id: "s1", customer: "Acme Corp", sent_by: "asha", sent_at: "2026-11-16T11:00:00Z", answers: 2, current: true };

  it("is not offered before a file exists for these decisions", () => {
    render(<FilePreview runId="r1" preview={preview()} />);

    expect(screen.queryByLabelText("Sent to")).toBeNull();
  });

  it("records the customer's name and then shows the record", async () => {
    const calls: { url: string; body: unknown }[] = [];
    vi.stubGlobal("fetch", async (url: string, init?: RequestInit) => {
      calls.push({ url, body: JSON.parse(String(init?.body)) });
      return Response.json(record, { status: 201 });
    });
    render(<FilePreview runId="r1" preview={preview({ latest })} />);

    await userEvent.type(screen.getByLabelText("Sent to"), "Acme Corp");
    await userEvent.click(screen.getByRole("button", { name: "Record as sent" }));

    expect(calls).toEqual([{ url: "/api/runs/r1/file/f1/sent", body: { customer: "Acme Corp" } }]);
    const item = await screen.findByText(/to Acme Corp on 16 Nov 2026 by asha · 2 answers/);
    expect(item.closest("li")).toHaveTextContent("Sent");
    expect(screen.getByLabelText("Sent to")).toHaveValue("");
    // Said aloud for someone who cannot see the new line appear.
    expect(screen.getByRole("status")).toHaveTextContent("Recorded as sent to Acme Corp.");
  });

  it("does not send an empty name", async () => {
    const fetched = vi.fn();
    vi.stubGlobal("fetch", fetched);
    render(<FilePreview runId="r1" preview={preview({ latest })} />);

    await userEvent.click(screen.getByRole("button", { name: "Record as sent" }));

    expect(fetched).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("Say who the file was sent to.");
    // The field is marked as the problem, described by the message, and has the focus.
    const field = screen.getByLabelText("Sent to");
    expect(field).toHaveAttribute("aria-invalid", "true");
    expect(field).toHaveAccessibleDescription("Say who the file was sent to.");
    expect(field).toHaveFocus();
  });

  it("lists earlier records, and says when one was for an older file", () => {
    const old = { ...record, id: "s0", customer: "Globex", current: false };
    render(<FilePreview runId="r1" preview={preview({ latest, sent: [old, record] })} />);

    const items = within(screen.getByRole("list", { name: "Recorded as sent" })).getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(items[0]).toHaveTextContent("to Globex");
    expect(items[0]).toHaveTextContent("before a decision changed");
    expect(items[1]).not.toHaveTextContent("before a decision changed");
  });

  it("shows the reason when recording is refused", async () => {
    vi.stubGlobal("fetch", async () => Response.json({ detail: "a decision changed after this file was made; download the file again" }, { status: 409 }));
    render(<FilePreview runId="r1" preview={preview({ latest })} />);

    await userEvent.type(screen.getByLabelText("Sent to"), "Acme");
    await userEvent.click(screen.getByRole("button", { name: "Record as sent" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("download the file again");
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
