// Words for what the API reports. A reviewer reads these, so they say what happened
// and, where it helps, what to do.
import type { Finding, Reopening, ReviewQuestion, RunQuestion } from "./api";

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
  daily_limit: "Stopped: today's spend ceiling was reached",
  stopped: "Stopped: an operator has paused this work",
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

// What every list and chip needs to know about a question. `reopened` comes with a row of
// the list; `reopening` with the question itself.
type Marked = { reopened?: boolean; reopening?: Reopening | null };
type Placed = Pick<RunQuestion, "status" | "review"> & Marked;

/** True while a question waits again because a live setting it rests on has changed. */
export function isReopened(question: Marked): boolean {
  return Boolean(question.reopened || question.reopening);
}

/** A setting, as every screen names it: the check, and what it was run on. */
export function settingName(checkName: string, scope: string): string {
  return `${checkName} on ${scope}`;
}

export function groupOf(question: Placed): Group {
  if (question.status === "queued" || question.status === "running") return "working";
  if (question.status === "failed" || question.status === "not_started") return "unanswered";
  if (question.review === "pending") {
    // A reopened question must be opened and read, whatever the agent made of its draft.
    return question.status === "ready" && !isReopened(question) ? "ready" : "needs";
  }
  return "decided";
}

export type Tone = "ready" | "review" | "fail" | "quiet";

export function chipFor(question: Placed): {
  tone: Tone;
  mark: string;
  label: string;
} {
  switch (groupOf(question)) {
    case "needs":
      return { tone: "review", mark: "!", label: isReopened(question) ? "Reopened" : "Needs review" };
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

/** True when nobody still has to act on any question: the filled file can be made. */
export function allDecided(questions: Pick<RunQuestion, "status" | "review">[]): boolean {
  if (questions.length === 0) return false;
  return questions.every(
    (question) =>
      question.status !== "queued" &&
      question.status !== "running" &&
      question.review !== "pending" &&
      question.review !== "redrafting",
  );
}

const EMPTY_REASONS: Record<string, { tone: Tone; mark: string; label: string; text: string }> = {
  rejected: { tone: "fail", mark: "×", label: "Rejected", text: "No approved answer, so the cell stays empty." },
  expired: { tone: "quiet", mark: "…", label: "Expired", text: "Nobody decided in time, so the cell stays empty." },
  unanswered: { tone: "fail", mark: "×", label: "Not answered", text: "The agent did not answer this question, so the cell stays empty." },
  undecided: { tone: "review", mark: "!", label: "Undecided", text: "This question still needs a decision." },
  no_answer: { tone: "review", mark: "!", label: "No wording", text: "The approved answer has no text." },
  no_answer_cell: { tone: "review", mark: "!", label: "No answer column", text: "The sheet has no answer column for this question. Copy the answer in by hand." },
  sheet_missing: { tone: "review", mark: "!", label: "Sheet not found", text: "The sheet this question came from is not in the file." },
  cell_not_empty: { tone: "review", mark: "!", label: "Cell not empty", text: "The cell already has content, and it is left as it is." },
  too_long: { tone: "review", mark: "!", label: "Too long", text: "The answer is longer than a cell can hold. It is not cut short." },
  bad_character: { tone: "review", mark: "!", label: "Cannot be stored", text: "The answer has a character a workbook cannot carry." },
};

/** Why a cell is left empty, in words. `detail` is what the API adds (a dropdown's values). */
export function emptyReason(reason: string, detail: string): { tone: Tone; mark: string; label: string; text: string } {
  if (reason === "dropdown") {
    const values = detail ? ` It accepts: ${detail}.` : " Its values could not be read.";
    return { tone: "review", mark: "!", label: "Dropdown", text: `The cell is a dropdown and the answer is not one of its values.${values}` };
  }
  return EMPTY_REASONS[reason] ?? { tone: "review", mark: "!", label: "Left empty", text: reason.replaceAll("_", " ") };
}

const FACTS: Record<string, string> = {
  protected: "Branch is protected",
  required_approving_reviews: "Approving reviews needed",
  applies_to_administrators: "Applies to administrators",
  default_branch: "Default branch",
  two_factor_required: "Two-factor sign-in required",
  secret_scanning_enabled: "Secret scanning switched on",
  root_mfa_enabled: "Root user has multi-factor sign-in",
  policy_set: "Password policy set",
  minimum_length: "Minimum password length",
  max_age_days: "Password expires after (days)",
  requires_symbols: "Passwords need a symbol",
  all_public_access_blocked: "All public access to S3 blocked",
  ebs_encrypted_by_default: "New disks encrypted by default",
  region: "Region",
};

/** A check's named fact, as a reviewer would say it. An unknown name is shown as it is. */
export function factLabel(fact: string): string {
  const words = fact.replaceAll("_", " ");
  // hasOwn: a fact named "constructor" must not find the object's own constructor.
  return Object.hasOwn(FACTS, fact) ? FACTS[fact]! : words.charAt(0).toUpperCase() + words.slice(1);
}

export function factValue(value: boolean | number | string | null | undefined): string {
  if (value === true) return "yes";
  if (value === false) return "no";
  if (value === null || value === undefined) return "not reported";
  return String(value);
}

/** The same text on the server, in the browser and in tests, whatever the time zone. */
export function utcDay(iso: string): string {
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
}

/** "to Acme Corp on 3 Nov 2026 and to Globex on 5 Nov 2026" */
export function sentList(sent: { customer: string; sent_at: string }[]): string {
  const each = sent.map((one) => `to ${one.customer} on ${utcDay(one.sent_at)}`);
  if (each.length <= 2) return each.join(" and ");
  return `${each.slice(0, -1).join(", ")} and ${each[each.length - 1]}`;
}

const FOUND: Record<string, string> = {
  webhook: "GitHub reported a change. Standingproof then read the setting itself; what GitHub sent is not used as a fact.",
  schedule: "Found by the daily look at this setting.",
  run: "Found when the agent looked at this setting while drafting.",
  manual: "Found by a look someone asked for.",
};

export function foundBy(trigger: string): string {
  return Object.hasOwn(FOUND, trigger) ? FOUND[trigger]! : "Found by a look at this setting.";
}

/** A moment, in UTC and saying so: the same text on the server and in any browser. */
export function utcMoment(iso: string): string {
  const time = new Date(iso).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  return `${utcDay(iso)}, ${time} UTC`;
}
