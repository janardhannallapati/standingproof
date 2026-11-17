import { describe, expect, it } from "vitest";

import { chipFor, groupOf, reasonText } from "@/lib/labels";

describe("where a question appears", () => {
  it.each([
    [{ status: "needs_review", review: "pending" }, "needs"],
    [{ status: "ready", review: "pending" }, "ready"],
    [{ status: "queued", review: null }, "working"],
    [{ status: "queued", review: "redrafting" }, "working"],
    [{ status: "running", review: null }, "working"],
    [{ status: "ready", review: "approved" }, "decided"],
    [{ status: "needs_review", review: "rejected" }, "decided"],
    [{ status: "failed", review: null }, "unanswered"],
    [{ status: "not_started", review: null }, "unanswered"],
  ] as const)("%o is in %s", (question, group) => {
    expect(groupOf(question)).toBe(group);
  });
});

describe("how a state is named", () => {
  it("always gives a word and a mark, never colour alone", () => {
    for (const review of ["pending", "approved", "edited", "rejected", "expired", "redrafting", null] as const) {
      for (const status of ["ready", "needs_review", "queued", "running", "failed", "not_started"] as const) {
        const chip = chipFor({ status, review });
        expect(chip.label.length).toBeGreaterThan(0);
        expect(chip.mark.length).toBeGreaterThan(0);
      }
    }
  });

  it("says an edited answer was approved", () => {
    expect(chipFor({ status: "ready", review: "edited" }).label).toBe("Edited and approved");
  });

  it("puts a reason in words, and survives one it has not seen", () => {
    expect(reasonText("no_approved_source")).toBe("No approved source");
    expect(reasonText("some_new_reason")).toBe("some new reason");
    expect(reasonText(null)).toBe("");
  });
});

describe("why our own rules stopped a question", () => {
  it.each([
    ["daily_limit", "Stopped: today's spend ceiling was reached"],
    ["stopped", "Stopped: an operator has paused this work"],
  ])("%s is said in words, not shown as a code", (reason, words) => {
    expect(reasonText(reason)).toBe(words);
  });
});
