import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { DecisionBar } from "@/components/DecisionBar";
import { QuestionList } from "@/components/QuestionList";
import { RunHeader } from "@/components/RunHeader";
import { SourceCard, citedRange } from "@/components/SourceCard";

import { detailed, listed, run } from "./fixtures";

describe("QuestionList", () => {
  const questions = [
    listed({ id: "a", position: 1 }),
    listed({ id: "b", position: 2, status: "needs_review", reason: "no_approved_source", answer: null }),
    listed({ id: "c", position: 3, review: "approved" }),
    listed({ id: "d", position: 4, status: "running", review: null }),
  ];

  it("puts what needs a person first, with counts", () => {
    render(<QuestionList questions={questions} selectedId="b" onSelect={() => {}} />);

    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual(["Needs review (1)", "Ready (1)", "In progress (1)", "Decided (1)"]);
  });

  it("says why a question needs review, in words", () => {
    render(<QuestionList questions={questions} selectedId={null} onSelect={() => {}} />);

    const needs = screen.getByRole("region", { name: "Needs review, 1" });
    expect(within(needs).getByText("No approved source")).toBeInTheDocument();
  });

  it("marks the open question and reports a click", async () => {
    const onSelect = vi.fn();
    render(<QuestionList questions={questions} selectedId="b" onSelect={onSelect} />);

    const rows = screen.getAllByRole("button");
    expect(rows.filter((row) => row.getAttribute("aria-current") === "true")).toHaveLength(1);
    await userEvent.click(screen.getByRole("button", { name: /^1/ }));
    expect(onSelect).toHaveBeenCalledWith("a");
  });

  it("leaves out a group with nothing in it", () => {
    render(<QuestionList questions={[listed()]} selectedId={null} onSelect={() => {}} />);

    expect(screen.getAllByRole("heading", { level: 2 })).toHaveLength(1);
  });
});

describe("SourceCard", () => {
  it("highlights the part of the source the answer relies on", () => {
    const source = detailed().sources[0]!;
    render(
      <SourceCard
        source={{ ...source, text: "Policy 4.2. MFA is required for all workforce members. SMS is not accepted." }}
        answer="Yes. MFA is required for all workforce members."
      />,
    );

    expect(screen.getByText("MFA is required for all workforce members.").tagName).toBe("MARK");
    expect(screen.getByText("owner: security")).toBeInTheDocument();
    expect(screen.getByText("valid until 1 Jan 2099")).toBeInTheDocument();
  });

  it("highlights nothing when the answer shares no sentence with the source", () => {
    expect(citedRange("We test backups yearly.", "Yes, MFA is on for everyone.")).toBeNull();
    expect(citedRange("anything", null)).toBeNull();
  });

  it("shows markup in a source as text", () => {
    const source = detailed().sources[0]!;
    const { container } = render(
      <SourceCard source={{ ...source, text: '<img src=x onerror="alert(1)"> hello' }} answer={null} />,
    );

    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText(/<img src=x/)).toBeInTheDocument();
  });

  it("says so when a cited source is gone", () => {
    render(
      <SourceCard
        source={{ id: "answer:x", available: false, kind: null, text: null, label: null, owner: null, valid_until: null, observed_at: null }}
        answer="Yes."
      />,
    );

    expect(screen.getByText(/can no longer be found/)).toBeInTheDocument();
  });
});

describe("RunHeader", () => {
  it("shows progress, what needs attention, and what it has cost", () => {
    render(<RunHeader run={run({ counts: { ready: 2, needs_review: 1, queued: 1 } })} filename="acme.xlsx" live needs={1} ready={2} />);

    expect(screen.getByText("3 of 4 answered")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "3");
    expect(screen.getByText("1 needs review")).toBeInTheDocument();
    expect(screen.getByText("2 ready")).toBeInTheDocument();
    expect(screen.getByText("$0.05 of $5.00")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Live");
  });

  it("says when it is not live", () => {
    render(<RunHeader run={run()} filename="acme.xlsx" live={false} needs={0} ready={0} />);

    expect(screen.getByRole("status")).toHaveTextContent("Not live: checking every 5 seconds");
  });
});

describe("DecisionBar", () => {
  function bar(question = detailed(), mode: "view" | "edit" | "reject" | "redraft" = "view") {
    const onDecide = vi.fn();
    const onMode = vi.fn();
    render(
      <DecisionBar question={question} mode={mode} onMode={onMode} onDecide={onDecide} busy={false} maxRedrafts={2} />,
    );
    return { onDecide, onMode };
  }

  it("approves the draft with one click", async () => {
    const { onDecide } = bar();

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(onDecide).toHaveBeenCalledWith("approve");
  });

  it("offers to write an answer, not to approve, when there is no draft", () => {
    bar(detailed({ answer: null, status: "needs_review" }));

    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
    expect(screen.getByRole("button", { name: "Write an answer" })).toBeInTheDocument();
  });

  it("starts an edit from the draft and sends the new wording", async () => {
    const { onDecide } = bar(detailed(), "edit");

    const field = screen.getByLabelText("Answer to approve");
    await userEvent.type(field, "Yes, including contractors.");
    await userEvent.click(screen.getByRole("button", { name: "Save and approve" }));

    expect(onDecide).toHaveBeenCalledWith("edit", { answer: "Yes, including contractors." });
  });

  it("needs an instruction before a redraft can be sent", async () => {
    const { onDecide } = bar(detailed(), "redraft");

    const send = screen.getByRole("button", { name: "Send back" });
    expect(send).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/do differently/), "Mention contractors.");
    await userEvent.click(send);

    expect(onDecide).toHaveBeenCalledWith("redraft", { comment: "Mention contractors." });
  });

  it("lets a rejection go without a comment", async () => {
    const { onDecide } = bar(detailed(), "reject");

    await userEvent.click(screen.getByRole("button", { name: "Reject" }));

    expect(onDecide).toHaveBeenCalledWith("reject", { comment: "" });
  });

  it("stops offering a redraft once the limit is reached", () => {
    bar(detailed({ redrafts: 2 }));

    expect(screen.getByRole("button", { name: "Ask for a redraft" })).toBeDisabled();
  });

  it("leaves the form on Escape", async () => {
    const { onMode } = bar(detailed(), "edit");

    await userEvent.type(screen.getByLabelText("Answer to approve"), "{Escape}");

    expect(onMode).toHaveBeenCalledWith("view");
  });
});
