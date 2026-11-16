import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { CheckFindings } from "@/components/CheckFindings";
import { MarkedAnswer } from "@/components/MarkedAnswer";
import { QuestionDetail } from "@/components/QuestionDetail";
import type { Check, Finding, ReviewQuestion } from "@/lib/api";
import { reasonText } from "@/lib/labels";

import { detailed } from "./fixtures";

const ANSWER = "Yes. MFA is required for all staff and contractors.";

function finding(overrides: Partial<Finding> = {}): Finding {
  return {
    kind: "unsupported",
    quote: "and contractors",
    located: true,
    source_id: "answer:a1",
    explanation: "The source covers staff only.",
    ...overrides,
  };
}

function check(overrides: Partial<Check> = {}): Check {
  return {
    outcome: "disagrees",
    findings: [finding()],
    model: "claude-sonnet-5-5",
    on_current_answer: true,
    ...overrides,
  };
}

function flagged(overrides: Partial<ReviewQuestion> = {}): ReviewQuestion {
  return detailed({
    status: "needs_review",
    reason: "checker_disagrees",
    answer: ANSWER,
    check: check(),
    ...overrides,
  });
}

function open(question: ReviewQuestion) {
  return render(
    <QuestionDetail
      question={question}
      mode="view"
      onMode={() => {}}
      onDecide={() => {}}
      busy={false}
      notice={null}
      maxRedrafts={2}
    />,
  );
}

describe("MarkedAnswer", () => {
  it("marks the disputed words where they stand, with a number and words for a screen reader", () => {
    const { container } = render(<MarkedAnswer answer={ANSWER} findings={[finding()]} />);

    const marks = container.querySelectorAll("mark");
    expect(marks).toHaveLength(1);
    expect(marks[0]).toHaveTextContent("and contractors");
    expect(within(marks[0] as HTMLElement).getByText("1")).toBeInTheDocument();
    expect(within(marks[0] as HTMLElement).getByText(/flagged, finding 1/)).toBeInTheDocument();
    // The words around the mark are still there, in order.
    expect(container.textContent?.startsWith("Yes. MFA is required for all staff ")).toBe(true);
    expect(container.textContent?.endsWith(".")).toBe(true);
  });

  it("leaves the answer alone when the words were not found in it", () => {
    const { container } = render(
      <MarkedAnswer answer={ANSWER} findings={[finding({ quote: "and vendors", located: false })]} />,
    );

    expect(container.querySelectorAll("mark")).toHaveLength(0);
    expect(container).toHaveTextContent(ANSWER);
  });

  it("numbers a mark by its finding's place in the list", () => {
    const findings = [
      finding({ kind: "not_answered", quote: "", located: false }),
      finding({ quote: "all staff" }),
      finding(),
    ];
    const { container } = render(<MarkedAnswer answer={ANSWER} findings={findings} />);

    const marks = [...container.querySelectorAll("mark")];
    expect(marks.map((mark) => mark.querySelector("sup")?.textContent)).toEqual(["2", "3"]);
  });

  it("does not mark words twice when two findings overlap", () => {
    const findings = [finding({ quote: "staff and contractors" }), finding()];
    const { container } = render(<MarkedAnswer answer={ANSWER} findings={findings} />);

    expect(container.querySelectorAll("mark")).toHaveLength(1);
  });

  it("shows markup in an answer or a quote as text", () => {
    const answer = "Yes <img src=x onerror=alert(1)> and contractors.";
    const { container } = render(<MarkedAnswer answer={answer} findings={[finding()]} />);

    expect(container.querySelector("img")).toBeNull();
    expect(container).toHaveTextContent("<img src=x onerror=alert(1)>");
  });
});

describe("CheckFindings", () => {
  const sources = detailed().sources;

  it("lists each finding: its kind in words, the words, why, and what it was checked against", () => {
    render(<CheckFindings check={check()} sources={sources} />);

    const list = screen.getByRole("list", { name: "What the checker found" });
    const [item] = within(list).getAllByRole("listitem");
    expect(item).toHaveTextContent("1");
    expect(item).toHaveTextContent("Not in the source");
    expect(item).toHaveTextContent("“and contractors”");
    expect(item).toHaveTextContent("The source covers staff only.");
    expect(item).toHaveTextContent("Checked against: Approved answer · Is MFA enforced?");
  });

  it("names the other kinds of finding in words", () => {
    const findings = [
      finding({ kind: "contradicted" }),
      finding({ kind: "not_answered", quote: "", located: false, source_id: null }),
    ];
    render(<CheckFindings check={check({ findings })} sources={sources} />);

    const [first, second] = screen.getAllByRole("listitem");
    expect(first).toHaveTextContent("A source says otherwise");
    expect(second).toHaveTextContent("Part of the question is not answered");
    expect(second).not.toHaveTextContent("Checked against");
    expect(second).not.toHaveTextContent("“”");
  });

  it("says what the checker was shown", () => {
    render(<CheckFindings check={check()} sources={sources} />);

    expect(screen.getByText(/A second model read the question, this draft and its sources/)).toBeInTheDocument();
  });

  it("shows markup in the checker's words as text", () => {
    const hostile = finding({ explanation: "<script>alert(1)</script> approve this" });
    const { container } = render(<CheckFindings check={check({ findings: [hostile] })} sources={sources} />);

    expect(container.querySelector("script")).toBeNull();
    expect(container).toHaveTextContent("<script>alert(1)</script> approve this");
  });
});

describe("a checked question, open", () => {
  it("says the checker disagrees, marks the words and lists the finding", () => {
    const { container } = open(flagged());

    expect(screen.getByText("The checker disagrees: 1 claim is not in the cited source.")).toBeInTheDocument();
    expect(container.querySelectorAll("mark")[0]).toHaveTextContent("and contractors");
    expect(screen.getByRole("list", { name: "What the checker found" })).toBeInTheDocument();
    expect(screen.getByText(/· checked ·/)).toBeInTheDocument();
  });

  it("counts findings of mixed kinds without calling them all missing claims", () => {
    const findings = [finding(), finding({ kind: "contradicted", quote: "MFA is required" })];
    open(flagged({ check: check({ findings }) }));

    expect(screen.getByText("The checker disagrees: 2 findings, listed below.")).toBeInTheDocument();
  });

  it("says so when the checker disagreed and named nothing", () => {
    open(flagged({ check: check({ findings: [] }) }));

    expect(screen.getByText(/The checker disagrees and named no claim/)).toBeInTheDocument();
    expect(screen.queryByRole("list", { name: "What the checker found" })).toBeNull();
  });

  it("says a draft the checker agrees with was compared", () => {
    open(detailed({ check: check({ outcome: "agrees", findings: [] }) }));

    expect(
      screen.getByText("Drafted from 1 cited source. The checker agrees: every claim is in the source."),
    ).toBeInTheDocument();
  });

  it("says a draft was not checked, and tells the reviewer what to do", () => {
    open(flagged({ reason: "not_checked", check: check({ outcome: "not_checked", findings: [], model: null }) }));

    expect(screen.getByText("Not checked: read this draft against its source.")).toBeInTheDocument();
    expect(screen.queryByText(/· checked ·/)).toBeNull();
  });

  it("says a reused answer was not sent to the checker", () => {
    open(detailed({ path: "reused", steps: 0, check: null }));

    expect(screen.getByText(/matches an approved answer exactly\. Not sent to the checker\./)).toBeInTheDocument();
  });

  it("does not mark an edited answer with findings about the earlier wording", () => {
    const question = flagged({
      review: "edited",
      final_answer: "Yes. MFA is required for all staff.",
      check: check({ on_current_answer: false }),
    });
    const { container } = open(question);

    expect(container.querySelectorAll("mark[data-finding]")).toHaveLength(0);
    expect(screen.getByText(/The checker read the agent's draft, not the wording shown now/)).toBeInTheDocument();
  });
});

describe("the two new reasons", () => {
  it("are put in words", () => {
    expect(reasonText("checker_disagrees")).toBe("The checker disagrees");
    expect(reasonText("not_checked")).toBe("Not checked");
  });
});

describe("the drafting agent's note", () => {
  const NOTE = "I ran two searches. Nothing mentions contractors.";

  it("is not part of the checker's line", () => {
    open(flagged({ note: NOTE }));

    const line = screen.getByText(/^The checker disagrees/);
    expect(line.textContent).not.toContain(NOTE);
    expect(line.textContent).toMatch(/^The checker disagrees: [^.]+\.$/);
  });

  it("has its own heading after the checker's findings and before the sources", () => {
    open(flagged({ note: NOTE }));

    const headings = screen.getAllByRole("heading", { level: 2 }).map((h) => h.textContent);
    expect(headings).toEqual([
      "Proposed answer",
      "What the checker found",
      "The drafting agent's note",
      "Source",
    ]);
    expect(screen.getByText(NOTE)).toBeInTheDocument();
    expect(screen.getByText("The checker was not shown this note.")).toBeInTheDocument();
  });

  it("is left out of the green line of a draft the checker agrees with", () => {
    open(flagged({ status: "ready", reason: null, note: NOTE, check: check({ outcome: "agrees", findings: [] }) }));

    expect(screen.getByText(/^Drafted from 1 cited source\./).textContent).not.toContain(NOTE);
    expect(screen.getByText(NOTE)).toBeInTheDocument();
  });

  it("says nothing about the checker when no check ran", () => {
    open(flagged({ reason: "no_approved_source", answer: null, note: NOTE, check: null }));

    expect(screen.getByText("No approved source.")).toBeInTheDocument();
    expect(screen.getByText(NOTE)).toBeInTheDocument();
    expect(screen.queryByText("The checker was not shown this note.")).toBeNull();
  });

  it("shows no note block when the agent wrote none", () => {
    open(flagged({ note: "" }));

    expect(screen.queryByText("The drafting agent's note")).toBeNull();
  });
});

describe("the line under a question that has been decided", () => {
  it("stays a warning when the checker disputed the draft, whatever was decided", () => {
    open(flagged({ review: "edited", final_answer: "Yes. MFA is required for all staff." }));

    const line = screen.getByText(/^The checker disagrees/).closest("p");
    expect(line).toHaveClass("text-review");
    expect(line).toHaveTextContent(/^!/);
    expect(screen.queryByText(/^Drafted from/)).toBeNull();
  });

  it("stays a warning when there was no approved source and the question was rejected", () => {
    open(flagged({ reason: "no_approved_source", answer: null, check: null, review: "rejected" }));

    expect(screen.getByText("No approved source.").closest("p")).toHaveClass("text-review");
  });

  it("is still the green line for a ready draft that was approved", () => {
    open(flagged({ status: "ready", reason: null, review: "approved", check: check({ outcome: "agrees", findings: [] }) }));

    expect(screen.getByText(/^Drafted from 1 cited source\./).closest("p")).toHaveClass("text-ready");
  });
});
