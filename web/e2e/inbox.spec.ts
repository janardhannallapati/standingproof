import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, expect, request, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
const SHEET = fileURLToPath(new URL("./fixtures/three-questions.xlsx", import.meta.url));
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

// Questions nothing else in the stack uses. If another approved answer to the same
// question existed with different wording, the two would disagree and neither would be
// reused, which is the product working as intended and not what this test is about.
const MFA = "Browser test: is multi-factor authentication required for employees?";
const ENCRYPTION = "Browser test: is customer data encrypted at rest?";
const INSURANCE = "Browser test: do you carry cyber liability insurance?";

type Listed = { id: string; question: string; status: string; review: string | null };

let api: APIRequestContext;
let runId: string;

async function questions(): Promise<Listed[]> {
  const response = await api.get(`/runs/${runId}/questions`);
  return (await response.json()).items;
}

/** A finished run of three questions: two with an exact approved answer, one without. */
test.beforeAll(async () => {
  api = await request.newContext({ baseURL: API });
  for (const [question, answer] of [
    [MFA, "Yes. Multi-factor authentication is required for all employee accounts."],
    [ENCRYPTION, "Yes. Customer data is encrypted at rest using AES-256."],
  ]) {
    const added = await api.post("/library/answers", {
      data: { question, answer, owner: "security", expires_on: "2099-01-01" },
    });
    expect(added.status()).toBe(201);
  }
  const uploaded = await api.post("/questionnaires", {
    multipart: { file: { name: "three-questions.xlsx", mimeType: XLSX, buffer: readFileSync(SHEET) } },
  });
  expect([200, 201]).toContain(uploaded.status());
  const started = await api.post(`/questionnaires/${(await uploaded.json()).id}/runs`);
  expect([200, 202]).toContain(started.status());
  runId = (await started.json()).id;
  await expect
    .poll(async () => (await (await api.get(`/runs/${runId}`)).json()).status, { timeout: 45_000 })
    .toBe("finished");
});

test.afterAll(async () => {
  await api.dispose();
});

for (const scheme of ["light", "dark"] as const) {
  test(`the inbox has no accessibility violations in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto(`/runs/${runId}`);
    await expect(page.getByRole("heading", { level: 1, name: INSURANCE })).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();

    const found = results.violations.map((v) => `${v.id}: ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
    expect(found).toEqual([]);
  });
}

test("the inbox is live, and puts what needs a person first with its reason", async ({ page }) => {
  await page.goto(`/runs/${runId}`);

  await expect(page.getByRole("heading", { level: 1, name: INSURANCE })).toBeVisible();
  await expect(page.getByRole("heading", { level: 2 }).first()).toHaveText("Needs review (1)");
  await expect(page.getByText("The model could not be called").first()).toBeVisible();
  // The browser's own connection to the API was accepted (its origin is on the list).
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
});

test("on a phone the list and the question are two screens", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 800 });
  await page.goto(`/runs/${runId}`);

  const row = page.getByRole("button", { name: new RegExp(MFA.slice(14, 44)) });
  await expect(row).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toBeHidden();

  await row.click();

  await expect(page.getByRole("heading", { level: 1, name: MFA })).toBeVisible();
  await expect(row).toBeHidden();
  for (const name of ["Approve", "Edit", "Reject", "Ask for a redraft"]) {
    const box = await page.getByRole("button", { name, exact: true }).boundingBox();
    expect(box?.height ?? 0, `${name} is tall enough to tap`).toBeGreaterThanOrEqual(44);
  }
  const sideways = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(sideways).toBeLessThanOrEqual(0);

  await page.getByRole("button", { name: "‹ All questions" }).click();
  await expect(row).toBeVisible();
});

test("a reviewer approves one, edits one and rejects one; each is recorded", async ({ page }) => {
  await page.goto(`/runs/${runId}`);
  await expect(page.getByText("Live", { exact: true })).toBeVisible();

  // Approve the first ready answer with the keyboard.
  await page.getByRole("button", { name: new RegExp(MFA.slice(14, 44)) }).click();
  await expect(page.getByRole("heading", { level: 1, name: MFA })).toBeVisible();
  await expect(page.getByText("Reused: the question matches an approved answer exactly.")).toBeVisible();
  await expect(page.locator("mark")).toHaveText(/Multi-factor authentication is required/);
  await page.keyboard.press("a");

  // The inbox moves on to the next waiting question by itself. Edit that one.
  await expect(page.getByRole("heading", { level: 1, name: ENCRYPTION })).toBeVisible();
  await page.keyboard.press("e");
  const field = page.getByLabel("Answer to approve");
  await field.fill("Yes. Customer data is encrypted at rest using AES-256, and we are certified.");
  await page.getByRole("button", { name: "Save and approve" }).click();
  // The framework keeps an alert region of its own for page changes, so ask for ours.
  await expect(page.getByRole("alert").filter({ hasText: "certified" })).toContainText(
    "no cited source uses the word “certified”",
  );

  // Reject the one that has no answer.
  await page.getByRole("button", { name: new RegExp(INSURANCE.slice(14, 44)) }).click();
  await expect(page.getByRole("heading", { level: 1, name: INSURANCE })).toBeVisible();
  await page.keyboard.press("r");
  await page.getByLabel(/Why is this rejected/).fill("Finance answers insurance questions.");
  await page.getByRole("button", { name: "Reject", exact: true }).click();

  await expect(page.getByRole("heading", { level: 2, name: "Decided (3)" })).toBeVisible();
  const reviews = Object.fromEntries((await questions()).map((q) => [q.question, q.review]));
  expect(reviews).toEqual({ [MFA]: "approved", [ENCRYPTION]: "edited", [INSURANCE]: "rejected" });
});

test("the reviewer downloads the spreadsheet with the approved answers in it, once", async ({ page }) => {
  await page.goto(`/runs/${runId}`);
  await page.getByRole("link", { name: "Filled file" }).click();
  await expect(page.getByRole("heading", { level: 1, name: "Filled file" })).toBeVisible();
  await expect(page.getByText(/^2 answers will be written\. 1 cell is left empty\./)).toBeVisible();
  const table = page.getByRole("table", { name: "What is written" });
  await expect(table.getByRole("row")).toHaveCount(3);
  await expect(page.getByRole("list", { name: "Left empty" }).getByRole("listitem")).toContainText(
    "Finance answers insurance questions.",
  );

  const waiting = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download filled file" }).click();
  const download = await waiting;

  expect(download.suggestedFilename()).toBe("three-questions-filled.xlsx");
  const bytes = readFileSync(await download.path());
  const { createHash } = await import("node:crypto");
  const latest = (await (await api.get(`/runs/${runId}/file/preview`)).json()).latest;
  expect(createHash("sha256").update(bytes).digest("hex")).toBe(latest.sha256);
  expect(latest.current).toBe(true);

  // A second download is the same file, not a second one.
  const again = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download filled file" }).click();
  const second = readFileSync(await (await again).path());
  expect(createHash("sha256").update(second).digest("hex")).toBe(latest.sha256);
  const made = await api.post(`/runs/${runId}/file`, { data: {} });
  expect(made.status()).toBe(200);
  expect((await made.json()).id).toBe(latest.id);
});

for (const scheme of ["light", "dark"] as const) {
  test(`the file page has no accessibility violations in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto(`/runs/${runId}/file`);
    await expect(page.getByRole("table", { name: "What is written" })).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();

    const found = results.violations.map((v) => `${v.id}: ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
    expect(found).toEqual([]);
  });
}

test("on a phone the file page does not scroll sideways; only its table does", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/runs/${runId}/file`);
  await expect(page.getByRole("button", { name: "Download filled file" })).toBeVisible();

  const wider = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);
  expect(wider).toBe(false);
  const box = await page.getByRole("button", { name: "Download filled file" }).boundingBox();
  expect(box!.height).toBeGreaterThanOrEqual(44);
});
