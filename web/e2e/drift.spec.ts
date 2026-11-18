import { createHmac, randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, type Page, expect, request, test } from "@playwright/test";

// Needs the stack started with compose.e2e.yaml on top of compose.yaml: it adds a scripted
// model and a fake GitHub, so this test takes the real path with no key and no network.
const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
const FAKES = process.env.E2E_FAKES_URL ?? "http://127.0.0.1:9100";
const SHEET = fileURLToPath(new URL("./fixtures/drift-question.xlsx", import.meta.url));
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
// The value in compose.e2e.yaml. A made-up test value.
const WEBHOOK_SECRET = "e2e-webhook-secret";

const QUESTION = "Drift test: do code changes need a second person's review before they are merged?";
const REPO = "e2e-org/app";
const SETTING = `github.branch_protection on ${REPO}`;
const NOT_PROTECTED = `Branch main of ${REPO} is not protected.`;

let api: APIRequestContext;
let fakes: APIRequestContext;
let runId: string;
let questionId: string;

async function question() {
  return (await api.get(`/run-questions/${questionId}`)).json();
}

async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).analyze();
  return results.violations.map((v) => `${v.id}: ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
}

/** What GitHub sends when a branch protection rule changes, signed as GitHub signs it. */
async function deliver() {
  const body = JSON.stringify({ action: "deleted", repository: { full_name: REPO } });
  return api.post("/webhooks/github", {
    headers: {
      "content-type": "application/json",
      "x-github-event": "branch_protection_rule",
      "x-github-delivery": randomUUID(),
      "x-hub-signature-256": `sha256=${createHmac("sha256", WEBHOOK_SECRET).update(body).digest("hex")}`,
    },
    data: body,
  });
}

/**
 * The whole path, with nothing written to the database by the test:
 * the worker drafts an answer from a look at (fake) GitHub, a reviewer approves it, makes
 * the file and records it as sent; branch protection is switched off; a signed delivery
 * arrives; the watcher looks for itself and reopens the answer.
 */
test.beforeAll(async () => {
  api = await request.newContext({ baseURL: API });
  fakes = await request.newContext({ baseURL: FAKES });
  expect((await fakes.post("/control/protection", { data: { reviews: 2 } })).status()).toBe(200);
  expect([200, 201]).toContain((await api.post("/connections", { data: { system: "github", scope: "e2e-org" } })).status());

  const uploaded = await api.post("/questionnaires", {
    multipart: { file: { name: "drift-question.xlsx", mimeType: XLSX, buffer: readFileSync(SHEET) } },
  });
  expect([200, 201]).toContain(uploaded.status());
  runId = (await (await api.post(`/questionnaires/${(await uploaded.json()).id}/runs`)).json()).id;
  await expect
    .poll(async () => (await (await api.get(`/runs/${runId}`)).json()).status, { timeout: 45_000 })
    .toBe("finished");
  const [listed] = (await (await api.get(`/runs/${runId}/questions`)).json()).items;
  questionId = listed.id;
  // The scripted model looked at branch protection and answered with what the look said.
  const drafted = await question();
  expect([drafted.status, drafted.path]).toEqual(["ready", "drafted"]);
  expect(drafted.answer).toContain("needs 2 approving reviews");
  expect(drafted.sources[0].kind).toBe("evidence");

  expect((await api.post(`/runs/${runId}/approve-ready`, { data: { count: 1 } })).status()).toBe(200);
  const file = await (await api.post(`/runs/${runId}/file`, { data: {} })).json();
  const sent = await api.post(`/runs/${runId}/file/${file.id}/sent`, { data: { customer: "Acme Corp" } });
  expect(sent.status()).toBe(201);

  // Branch protection is switched off, and GitHub says something changed.
  await fakes.post("/control/protection", { data: { reviews: null } });
  expect((await deliver()).status()).toBe(202);
  await expect.poll(async () => (await question()).reopening !== null, { timeout: 30_000 }).toBe(true);
});

test.afterAll(async () => {
  // Leave the fake as the next run of this file expects to find it.
  await fakes.post("/control/protection", { data: { reviews: 2 } });
  await fakes.dispose();
  await api.dispose();
});

test("a delivery that is not signed with the secret is refused", async () => {
  const refused = await api.post("/webhooks/github", {
    headers: {
      "content-type": "application/json",
      "x-github-event": "branch_protection_rule",
      "x-github-delivery": randomUUID(),
      "x-hub-signature-256": `sha256=${"0".repeat(64)}`,
    },
    data: "{}",
  });
  expect(refused.status()).toBe(401);
});

test("the runs list marks the run, and the Changes page leads to the question", async ({ page }) => {
  await page.goto("/runs");
  // The newest run is first. On a stack these tests have run on before, an earlier run's
  // answer rests on the same setting and is, rightly, reopened as well.
  await expect(page.getByRole("link", { name: /drift-question\.xlsx.*1 answer reopened/ }).first()).toBeVisible();
  // So the change may have reopened more than this run's one answer.
  // The page's text runs on with no space after it ("reopenedseen 18 Nov").
  const reopenedCount = /[1-9]\d* answers? reopened/;

  await page.getByRole("link", { name: "Changes" }).click();
  const entry = page.getByRole("listitem").filter({ hasText: SETTING }).first();
  await expect(entry).toContainText(reopenedCount);
  await expect(entry).toContainText(NOT_PROTECTED);
  await expect(entry).toContainText("Approving reviews needed: was 2, now 0");
  await expect(entry).toContainText("Sent to Acme Corp");

  // One link for each answer the change reopened; the newest run's is first.
  await entry.getByRole("link", { name: /Drift test: do code changes need/ }).first().click();
  await expect(page.getByRole("heading", { level: 1, name: QUESTION })).toBeVisible();
});

test("the question says what changed and who has the answer, and keeping it puts the file back", async ({ page }) => {
  await page.goto(`/runs/${runId}?question=${questionId}`);
  await expect(page.getByText("Live", { exact: true })).toBeVisible();

  await expect(page.getByRole("region", { name: "Needs review, 1" })).toContainText("Reopened");
  await expect(
    page.getByText("Reopened: a live setting this answer rests on has changed since it was approved."),
  ).toBeVisible();
  const table = page.getByRole("table", { name: SETTING });
  await expect(table.getByRole("row", { name: /Approving reviews needed/ })).toContainText("changed: 0");
  await expect(page.getByText(/GitHub reported a change/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "The answer that was sent" })).toBeVisible();
  await expect(page.getByText(/^Sent with this wording to Acme Corp on /)).toBeVisible();
  await expect(page.getByText(/the setting has changed since/)).toBeVisible();
  // No file while it waits.
  await expect(page.getByRole("link", { name: "Filled file" })).toHaveCount(0);

  // "A" approves a draft elsewhere. Here it must do nothing.
  await expect(page.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
  await page.keyboard.press("a");
  expect((await question()).review).toBe("pending");

  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    expect(await violations(page), `reopened question, ${scheme}`).toEqual([]);
  }

  await page.getByRole("button", { name: "Keep this answer" }).click();

  await expect(page.getByRole("link", { name: "Filled file" })).toBeVisible();
  const after = await question();
  expect([after.review, after.reopening]).toEqual(["approved", null]);
  // The file made before is the file for these decisions again.
  const preview = await (await api.get(`/runs/${runId}/file/preview`)).json();
  expect([preview.ready, preview.latest.current, preview.sent[0].current]).toEqual([true, true, true]);
});

for (const scheme of ["light", "dark"] as const) {
  test(`the Changes page has no accessibility violations in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto("/changes");
    await expect(page.getByRole("listitem").filter({ hasText: SETTING }).first()).toBeVisible();

    expect(await violations(page)).toEqual([]);
  });
}

test("on a phone the Changes page and its links fit the screen", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/changes");
  const link = page.getByRole("link", { name: /Drift test: do code changes need/ }).first();
  await expect(link).toBeVisible();

  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  for (const target of [link, page.getByRole("link", { name: "Runs" })]) {
    expect((await target.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
});
