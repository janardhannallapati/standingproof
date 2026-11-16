import type { Change, Reopening, ReviewQuestion, Run, RunQuestion } from "@/lib/api";

export function listed(overrides: Partial<RunQuestion> = {}): RunQuestion {
  return {
    id: "q-1",
    position: 1,
    question: "Is MFA enforced?",
    status: "ready",
    path: "drafted",
    reason: null,
    answer: "Yes, for all staff.",
    citations: ["answer:a1"],
    note: "",
    steps: 2,
    cost_usd: "0.016900",
    review: "pending",
    version: 1,
    reopened: false,
    ...overrides,
  };
}

export function detailed(overrides: Partial<ReviewQuestion> = {}): ReviewQuestion {
  return {
    id: "q-1",
    run_id: "run-1",
    position: 1,
    question: "Is MFA enforced?",
    status: "ready",
    path: "drafted",
    reason: null,
    answer: "Yes, for all staff.",
    note: "",
    sources: [
      {
        id: "answer:a1",
        available: true,
        kind: "answer",
        text: "Yes, for all staff.",
        label: "Is MFA enforced?",
        owner: "security",
        valid_until: "2099-01-01",
        observed_at: null,
      },
    ],
    review: "pending",
    final_answer: null,
    version: 1,
    redrafts: 0,
    steps: 2,
    cost_usd: "0.016900",
    warnings: [],
    reopening: null,
    ...overrides,
  };
}

export function run(overrides: Partial<Run> = {}): Run {
  return {
    id: "run-1",
    questionnaire_id: "sheet-1",
    status: "finished",
    counts: { ready: 2, needs_review: 1 },
    cost_usd: "0.050000",
    max_cost_usd: "5.0000",
    created_at: "2026-11-16T01:00:00Z",
    finished_at: "2026-11-16T01:01:00Z",
    ...overrides,
  };
}

/** Branch protection was switched off after the answer was approved and sent. */
export function reopening(overrides: Partial<Reopening> = {}): Reopening {
  return {
    reopened_at: "2026-11-16T11:40:05Z",
    was_review: "approved",
    settings: [
      {
        check_name: "github.branch_protection",
        scope: "acme/app",
        was: {
          statement: "Branch main of acme/app is protected: a change needs 2 approving reviews.",
          observed_at: "2026-11-03T09:12:00Z",
        },
        now: { statement: "Branch main of acme/app is not protected.", observed_at: "2026-11-16T11:40:00Z" },
        changes: [
          { fact: "protected", was: true, now: false },
          { fact: "required_approving_reviews", was: 2, now: 0 },
        ],
        trigger: "webhook",
      },
    ],
    sent_to: [
      { customer: "Acme Corp", sent_at: "2026-11-03T10:02:00Z" },
      { customer: "Globex", sent_at: "2026-11-05T14:30:00Z" },
    ],
    ...overrides,
  };
}

/** An approved answer that is waiting again. */
export function reopened(overrides: Partial<ReviewQuestion> = {}): ReviewQuestion {
  return detailed({
    review: "pending",
    final_answer: "Yes. Two approving reviews are required.",
    answer: "Yes, two reviews.",
    version: 3,
    reopening: reopening(),
    ...overrides,
  });
}

export function change(overrides: Partial<Change> = {}): Change {
  return {
    ...reopening().settings[0]!,
    id: "change-1",
    detected_at: "2026-11-16T11:40:05Z",
    answers: [
      {
        run_question_id: "q-1",
        run_id: "run-1",
        filename: "acme-vendor.xlsx",
        position: 1,
        question: "Do code changes need review?",
        waiting: true,
        sent_to: reopening().sent_to,
      },
    ],
    ...overrides,
  };
}
