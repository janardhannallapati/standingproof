import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ChangesList } from "@/components/ChangesList";
import { DecisionBar } from "@/components/DecisionBar";
import { FilePreview } from "@/components/FilePreview";
import { Inbox } from "@/components/Inbox";
import { QuestionDetail } from "@/components/QuestionDetail";
import { QuestionList } from "@/components/QuestionList";
import { RunsList } from "@/components/RunsList";
import { WhatChanged } from "@/components/WhatChanged";
import type { ReviewQuestion } from "@/lib/api";
import { chipFor, factLabel, factValue, groupOf } from "@/lib/labels";

import { change, detailed, listed, reopened, reopening, run } from "./fixtures";

afterEach(() => vi.unstubAllGlobals());

describe("a reopened question, in words", () => {
  it("needs a person even when the agent's draft was ready", () => {
    const question = { status: "ready", review: "pending", reopened: true } as const;

    expect(groupOf(question)).toBe("needs");
    expect(chipFor(question)).toEqual({ tone: "review", mark: "!", label: "Reopened" });
    expect(groupOf({ status: "ready", review: "pending", reopened: false })).toBe("ready");
  });

  it("names a check's facts the way a reviewer would", () => {
    expect(factLabel("required_approving_reviews")).toBe("Approving reviews needed");
    expect(factLabel("something_new")).toBe("Something new");
    expect([factValue(true), factValue(false), factValue(2), factValue(null), factValue("main")]).toEqual([
      "yes",
      "no",
      "2",
      "not reported",
      "main",
    ]);
  });
});

describe("QuestionList", () => {
  it("puts a reopened question under Needs review and says why", () => {
    render(
      <QuestionList
        questions={[listed({ id: "a", reopened: true }), listed({ id: "b", position: 2 })]}
        selectedId={null}
        onSelect={() => {}}
      />,
    );

    const needs = screen.getByRole("region", { name: "Needs review, 1" });
    expect(within(needs).getByText("Reopened")).toBeInTheDocument();
    expect(within(needs).getByText("a live setting changed")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Ready, 1" })).toBeInTheDocument();
  });
});

describe("WhatChanged", () => {
  it("is a table of each changed setting, when approved and now", () => {
    render(<WhatChanged reopening={reopening()} />);

    const table = screen.getByRole("table", { name: "github.branch_protection on acme/app" });
    expect(within(table).getByRole("columnheader", { name: /When approved.*3 Nov 2026/ })).toBeInTheDocument();
    expect(within(table).getByRole("columnheader", { name: /Now.*16 Nov 2026/ })).toBeInTheDocument();
    const row = within(table).getByRole("row", { name: /Approving reviews needed/ });
    expect(within(row).getAllByRole("cell").map((cell) => cell.textContent)).toEqual(["2", "! changed: 0"]);
    expect(screen.getByText("Branch main of acme/app is not protected.")).toBeInTheDocument();
    expect(screen.getByText(/GitHub reported a change.*read the setting itself/)).toBeInTheDocument();
  });

  it("says when approved or when drafted, and how the look came about", () => {
    const daily = reopening({ was_review: null });
    daily.settings[0]!.trigger = "schedule";
    render(<WhatChanged reopening={daily} />);

    expect(screen.getByRole("columnheader", { name: /When drafted/ })).toBeInTheDocument();
    expect(screen.getByText(/Found by the daily look/)).toBeInTheDocument();
  });

  it("says so when the setting changed and changed back", () => {
    const back = reopening();
    back.settings[0]!.changes = [];
    render(<WhatChanged reopening={back} />);

    expect(screen.getByText(/changed and has changed back/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("QuestionDetail, reopened", () => {
  const show = (question: ReviewQuestion) =>
    render(
      <QuestionDetail question={question} mode="view" onMode={() => {}} onDecide={() => {}} busy={false} notice={null} maxRedrafts={2} />,
    );

  it("says why it is back, what was sent and to whom", () => {
    show(reopened());

    expect(
      screen.getByText("Reopened: a live setting this answer rests on has changed since it was approved."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "The answer that was sent" })).toBeInTheDocument();
    expect(screen.getByText("Yes. Two approving reviews are required.")).toBeInTheDocument();
    expect(
      screen.getByText("Sent with this wording to Acme Corp on 3 Nov 2026 and to Globex on 5 Nov 2026."),
    ).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "What changed" })).toBeInTheDocument();
  });

  it("does not say sent for an answer that was approved and not sent", () => {
    show(reopened({ reopening: reopening({ sent_to: [] }) }));

    expect(screen.getByRole("heading", { name: "The approved answer" })).toBeInTheDocument();
    expect(screen.queryByText(/Sent with this wording/)).toBeNull();
  });

  it("speaks of a draft when nobody had decided it", () => {
    show(reopened({ final_answer: null, reopening: reopening({ was_review: null, sent_to: [] }) }));

    expect(screen.getByText(/has changed since it was drafted\./)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Proposed answer" })).toBeInTheDocument();
  });
});

describe("DecisionBar, reopened", () => {
  function bar(question = reopened(), mode: "view" | "edit" | "reject" | "redraft" = "view") {
    const onDecide = vi.fn();
    const onMode = vi.fn();
    render(<DecisionBar question={question} mode={mode} onMode={onMode} onDecide={onDecide} busy={false} maxRedrafts={2} />);
    return { onDecide, onMode };
  }

  it("offers edit, keep, withdraw and redraft, and not approve", async () => {
    const { onDecide } = bar();

    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "EditE",
      "Keep this answer",
      "WithdrawR",
      "Ask for a redraftD",
    ]);
    await userEvent.click(screen.getByRole("button", { name: "Keep this answer" }));
    expect(onDecide).toHaveBeenCalledWith("keep");
  });

  it("starts an edit from the approved wording, not from the agent's draft", async () => {
    function Bar() {
      const [mode, setMode] = useState<"view" | "edit" | "reject" | "redraft">("view");
      return <DecisionBar question={reopened()} mode={mode} onMode={setMode} onDecide={() => {}} busy={false} maxRedrafts={2} />;
    }
    render(<Bar />);

    await userEvent.click(screen.getByRole("button", { name: "Edit" }));

    expect(screen.getByLabelText("Answer to approve")).toHaveValue("Yes. Two approving reviews are required.");
  });

  it("calls a rejection a withdrawal", () => {
    bar(reopened(), "reject");

    expect(screen.getByLabelText("Why is this withdrawn? (optional)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Withdraw" })).toBeInTheDocument();
    expect(screen.getByText(/cell is left empty in the next file/)).toBeInTheDocument();
  });
});

describe("the inbox with a reopened question", () => {
  function open(rows: ReviewQuestion[]) {
    const posts: string[] = [];
    vi.stubGlobal("WebSocket", class { close() {} });
    vi.stubGlobal("fetch", async (input: string, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "POST") posts.push(url);
      const one = url.match(/\/run-questions\/([^/?]+)$/);
      const body = one
        ? rows.find((q) => q.id === one[1])
        : url.includes("/questions")
          ? { items: rows.map((q) => listed({ id: q.id, position: q.position, question: q.question, reopened: Boolean(q.reopening) })), next_after: null }
          : run();
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
        <Inbox
          run={run()}
          questions={rows.map((q) => listed({ id: q.id, position: q.position, question: q.question, reopened: Boolean(q.reopening) }))}
          filename="acme.xlsx"
          eventsUrl={null}
        />
      </QueryClientProvider>,
    );
    return posts;
  }

  it("does not keep or approve it from the keyboard, and leaves it out of approve all", async () => {
    const posts = open([reopened({ id: "back", question: "Do changes need review?" }), detailed({ id: "new", position: 2 })]);
    await screen.findByRole("heading", { level: 1, name: "Do changes need review?" });
    await screen.findByRole("button", { name: "Keep this answer" });

    await userEvent.keyboard("a");

    expect(posts).toEqual([]);
    expect(screen.getByRole("button", { name: "Approve all 1" })).toBeInTheDocument();
  });
});

describe("FilePreview while a question waits again", () => {
  it("lists who was sent a file that is now out of date", () => {
    const sent = [
      { id: "s1", customer: "Acme Corp", sent_by: "asha", sent_at: "2026-11-03T10:02:00Z", answers: 3, current: false, reopened: 1 },
    ];
    render(
      <FilePreview
        runId="r1"
        preview={{ filename: "a-filled.xlsx", ready: false, undecided: 1, written: [], left_empty: [], protected_sheets: [], latest: null, sent }}
      />,
    );

    const list = screen.getByRole("list", { name: "Sent before this change" });
    expect(within(list).getByText("Out of date")).toBeInTheDocument();
    expect(within(list).getByRole("listitem")).toHaveTextContent(
      "to Acme Corp on 3 Nov 2026 by asha · 3 answers, 1 reopened since",
    );
    expect(screen.getByText(/One answer was reopened because a live setting changed/)).toBeInTheDocument();
  });

  it("shows no such list when nothing was sent", () => {
    render(
      <FilePreview
        runId="r1"
        preview={{ filename: "a-filled.xlsx", ready: false, undecided: 2, written: [], left_empty: [], protected_sheets: [], latest: null, sent: [] }}
      />,
    );

    expect(screen.queryByRole("list", { name: "Sent before this change" })).toBeNull();
  });
});

describe("RunsList", () => {
  const item = { id: "run-1", questionnaire_id: "s", filename: "acme.xlsx", status: "finished", created_at: "2026-11-03T09:10:00Z", finished_at: null } as const;

  it("marks a run that has reopened answers", () => {
    render(<RunsList runs={[{ ...item, reopened: 2 }, { ...item, id: "run-2", filename: "globex.xlsx", reopened: 0 }]} />);

    const [first, second] = screen.getAllByRole("listitem");
    expect(first).toHaveTextContent("2 answers reopened");
    expect(second).not.toHaveTextContent("reopened");
  });
});

describe("ChangesList", () => {
  it("lists each change with the answers it reopened and who has them", () => {
    render(<ChangesList changes={[change(), change({ id: "c2", check_name: "github.secret_scanning", answers: [], changes: [] })]} />);

    const [first, second] = screen.getAllByRole("listitem") as [HTMLElement, HTMLElement];
    expect(within(first).getByRole("heading", { name: "github.branch_protection on acme/app" })).toBeInTheDocument();
    expect(first).toHaveTextContent("1 answer reopened");
    expect(first).toHaveTextContent("Approving reviews needed: was 2, now 0");
    expect(first).toHaveTextContent("Sent to Acme Corp on 3 Nov 2026 and to Globex on 5 Nov 2026");
    expect(within(first).getByRole("link", { name: /question 1: Do code changes need review\?/ })).toHaveAttribute(
      "href",
      "/runs/run-1?question=q-1",
    );
    expect(second).toHaveTextContent("No answer rests on it");
  });

  it("says when an answer has been decided again", () => {
    const decided = change();
    decided.answers[0]!.waiting = false;
    render(<ChangesList changes={[decided]} />);

    expect(screen.getByRole("listitem")).toHaveTextContent("1 answer reopened, since decided");
  });

  it("says so when nothing has changed", () => {
    render(<ChangesList changes={[]} />);

    expect(screen.getByText(/No live setting has been seen to change/)).toBeInTheDocument();
  });
});
