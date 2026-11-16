// Words for what the API reports. A reviewer reads these, so they say what happened
// and, where it helps, what to do.
import type { Finding, ReviewQuestion, RunQuestion } from "./api";

const REASONS: Record<string, string> = {
  no_approved_source: "No approved source",
  ungrounded_answer: "The draft cited a source no search returned",
  unsupported_claim: "The draft claimed more than its sources say",
  step_limit: "Stopped at the step limit",
  cost_limit: "Stopped at the cost limit",
  time_limit: "Stopped at the time limit",
  repeated_call: "The agent repeated the same search",
  model_declined: "The model declined to answer",
  output_truncated: "The model's reply was cut off",
  unexpected_stop: "The model stopped unexpectedly",
  model_error: "The model could not be called",
  model_unavailable: "The model provider was unavailable",
  checker_disagrees: "The checker disagrees",
  not_checked: "Not checked",
  redraft_failed: "The redraft could not be made; the earlier draft is shown",
  run_cost_limit: "The run reached its cost ceiling first",
  cancelled: "The run was cancelled",
  attempts_exhausted: "Gave up after three attempts",
  internal_error: "Something went wrong on our side",
  worker_stopped: "Waiting for a worker",
};

export function reasonText(reason: string | null | undefined): string {
  if (!reason) return "";
  return REASONS[reason] ?? reason.replaceAll("_", " ");
}

export const SOURCE_KINDS: Record<string, string> = {
  answer: "Approved answer",
  policy: "Policy",
  evidence: "Live check",
};

export const FINDING_KINDS: Record<Finding["kind"], string> = {
  unsupported: "Not in the source",
  contradicted: "A source says otherwise",
  not_answered: "Part of the question is not answered",
};

/**
 * What the independent checker concluded, as one sentence. Null when the draft was not
 * sent to it. "Agrees" is as far as this goes: a second reader agreeing is not proof.
 */
export function checkLine(check: ReviewQuestion["check"]): string | null {
  if (!check) return null;
  if (check.outcome === "agrees") return "The checker agrees: every claim is in the source.";
  if (check.outcome === "not_checked") return "Not checked: read this draft against its source.";
  const count = check.findings.length;
  if (count === 0) return "The checker disagrees and named no claim. Read this draft against its source.";
  if (check.findings.every((finding) => finding.kind === "unsupported")) {
    return `The checker disagrees: ${count} claim${count === 1 ? " is" : "s are"} not in the cited source.`;
  }
  return `The checker disagrees: ${count} finding${count === 1 ? "" : "s"}, listed below.`;
}

export type Group = "needs" | "ready" | "working" | "decided" | "unanswered";

export const GROUP_TITLES: Record<Group, string> = {
  needs: "Needs review",
  ready: "Ready",
  working: "In progress",
  decided: "Decided",
  unanswered: "Not answered",
};

// The order the groups appear in: what needs a person first.
export const GROUP_ORDER: Group[] = ["needs", "ready", "working", "decided", "unanswered"];

export function groupOf(question: Pick<RunQuestion, "status" | "review">): Group {
  if (question.status === "queued" || question.status === "running") return "working";
  if (question.status === "failed" || question.status === "not_started") return "unanswered";
  if (question.review === "pending") return question.status === "ready" ? "ready" : "needs";
  return "decided";
}

export type Tone = "ready" | "review" | "fail" | "quiet";

export function chipFor(question: Pick<RunQuestion, "status" | "review">): {
  tone: Tone;
  mark: string;
  label: string;
} {
  switch (groupOf(question)) {
    case "needs":
      return { tone: "review", mark: "!", label: "Needs review" };
    case "ready":
      return { tone: "ready", mark: "✓", label: "Ready" };
    case "working":
      return { tone: "quiet", mark: "…", label: question.review === "redrafting" ? "Redrafting" : "In progress" };
    case "unanswered":
      return { tone: "fail", mark: "×", label: question.status === "failed" ? "Failed" : "Not started" };
    case "decided":
      if (question.review === "rejected") return { tone: "fail", mark: "×", label: "Rejected" };
      if (question.review === "expired") return { tone: "quiet", mark: "–", label: "Expired" };
      return { tone: "ready", mark: "✓", label: question.review === "edited" ? "Edited and approved" : "Approved" };
  }
}

export function money(value: string | number): string {
  return `$${Number(value).toFixed(2)}`;
}

export function fineMoney(value: string | number): string {
  return `$${Number(value).toFixed(4)}`;
}
