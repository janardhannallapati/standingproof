import type { ReviewQuestion, Run, RunQuestion } from "@/lib/api";

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
