import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, type Page, expect, request, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
const SHEET = fileURLToPath(new URL("./fixtures/three-questions.xlsx", import.meta.url));
const REPO_ROOT = fileURLToPath(new URL("../..", import.meta.url));
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

const MFA = "Browser test: is multi-factor authentication required for employees?";
const ENCRYPTION = "Browser test: is customer data encrypted at rest?";
const SETTING = "github.org_two_factor on e2e-org";
const REQUIRED = "The GitHub organisation e2e-org requires two-factor sign-in for its members.";
const NOT_REQUIRED = "The GitHub organisation e2e-org does not require two-factor sign-in for its members.";

let api: APIRequestContext;
let runId: string;
let questionId: string;

/**
 * One statement against the stack's own database.
 *
 * An answer rests on a live check only when a model drafted it from one, and this stack has
 * no model. So the test writes the two rows a model run and a later look would have left:
 * an observation the answer cites, and a newer one that reads differently. Everything from
 * there on is the product: the watcher compares them, and the API and the screen say so.
 */
function sql(statement: string) {
  const project = process.env.E2E_COMPOSE_PROJECT;
  execFileSync(
    "docker",
    ["compose", ...(project ? ["-p", project] : []), "exec", "-T", "postgres", "psql", "-U", "standingproof", "-d", "standingproof", "-v", "ON_ERROR_STOP=1", "-c", statement],
    { cwd: REPO_ROOT, stdio: "pipe" },
  );
}

function observed(facts: string, statement: string, when: string): string {
  return `INSERT INTO evidence (tenant_id, check_name, scope, status, facts, raw_result, statement, observed_at)
    SELECT tenant_id, 'github.org_two_factor', 'e2e-org', 'observed', '${facts}', '{}', '${statement}', ${when}
    FROM runs WHERE id = '${runId}' RETURNING id, tenant_id`;
}

async function question() {
  return (await api.get(`/run-questions/${questionId}`)).json();
}

async function violations(page: Page): Promise<string[]> {
  const results = await new AxeBuilder({ page }).analyze();
  return results.violations.map((v) => `${v.id}: ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
}

/** An approved answer, recorded as sent, whose live setting then reads differently. */
test.beforeAll(async () => {
  api = await request.newContext({ baseURL: API });
  for (const [asked, answer] of [
    [MFA, "Yes. Multi-factor authentication is required for all employee accounts."],
    [ENCRYPTION, "Yes. Customer data is encrypted at rest using AES-256."],
  ]) {
    await api.post("/library/answers", { data: { question: asked, answer, owner: "security", expires_on: "2099-01-01" } });
  }
  const uploaded = await api.post("/questionnaires", {
    multipart: { file: { name: "three-questions.xlsx", mimeType: XLSX, buffer: readFileSync(SHEET) } },
  });
  runId = (await (await api.post(`/questionnaires/${(await uploaded.json()).id}/runs`)).json()).id;
  expect(runId).toMatch(/^[0-9a-f-]{36}$/);
  await expect
    .poll(async () => (await (await api.get(`/runs/${runId}`)).json()).status, { timeout: 45_000 })
    .toBe("finished");
  const listed = (await (await api.get(`/runs/${runId}/questions`)).json()).items;
  questionId = listed.find((q: { question: string }) => q.question === MFA).id;

  // What a model run would have left: the answer cites a look at the live setting.
  sql(`WITH seen AS (${observed('{"two_factor_required": true}', REQUIRED, "now() - interval '14 days'")}),
    cited AS (UPDATE run_questions rq SET citations = jsonb_build_array('evidence:' || seen.id)
      FROM seen WHERE rq.id = '${questionId}' RETURNING rq.id, seen.id AS evidence_id, seen.tenant_id)
    INSERT INTO answer_evidence (run_question_id, check_name, scope, tenant_id, evidence_id)
    SELECT id, 'github.org_two_factor', 'e2e-org', tenant_id, evidence_id FROM cited`);

  expect((await api.post(`/runs/${runId}/approve-ready`, { data: { count: 2 } })).status()).toBe(200);
  const third = listed.find((q: { status: string }) => q.status === "needs_review");
  const rejected = await api.post(`/run-questions/${third.id}/decision`, {
    data: { decision: "reject", version: third.version, comment: "Finance answers this." },
  });
  expect(rejected.status()).toBe(200);
  const file = await (await api.post(`/runs/${runId}/file`, { data: {} })).json();
  const sent = await api.post(`/runs/${runId}/file/${file.id}/sent`, { data: { customer: "Acme Corp" } });
  expect(sent.status()).toBe(201);

  // What a later look would have left: the setting now reads differently.
  sql(`WITH seen AS (${observed('{"two_factor_required": false}', NOT_REQUIRED, "now()")})
    INSERT INTO drift_queue (evidence_id, tenant_id, trigger) SELECT id, tenant_id, 'schedule' FROM seen`);
  // The watcher service compares them on its next pass.
  await expect.poll(async () => (await question()).reopening !== null, { timeout: 30_000 }).toBe(true);
});

test.afterAll(async () => {
  await api.dispose();
});

test("the runs list marks the run, and the Changes page leads to the question", async ({ page }) => {
  await page.goto("/runs");
  await expect(page.getByRole("link", { name: /three-questions\.xlsx.*1 answer reopened/ })).toBeVisible();

  await page.getByRole("link", { name: "Changes" }).click();
  const entry = page.getByRole("listitem").filter({ hasText: SETTING }).first();
  await expect(entry).toContainText("1 answer reopened");
  await expect(entry).toContainText(NOT_REQUIRED);
  await expect(entry).toContainText("Two-factor sign-in required: was yes, now no");
  await expect(entry).toContainText("Sent to Acme Corp");

  await entry.getByRole("link", { name: new RegExp(MFA.slice(14, 44)) }).click();
  await expect(page.getByRole("heading", { level: 1, name: MFA })).toBeVisible();
});

test("the question says what changed and who has the answer, and keeping it puts the file back", async ({ page }) => {
  await page.goto(`/runs/${runId}?question=${questionId}`);
  await expect(page.getByText("Live", { exact: true })).toBeVisible();

  await expect(page.getByRole("region", { name: "Needs review, 1" })).toContainText("Reopened");
  await expect(
    page.getByText("Reopened: a live setting this answer rests on has changed since it was approved."),
  ).toBeVisible();
  const table = page.getByRole("table", { name: SETTING });
  await expect(table.getByRole("row", { name: /Two-factor sign-in required/ })).toContainText("changed: no");
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
  const link = page.getByRole("link", { name: new RegExp(MFA.slice(14, 44)) }).first();
  await expect(link).toBeVisible();

  expect(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth)).toBe(false);
  for (const target of [link, page.getByRole("link", { name: "Runs" })]) {
    expect((await target.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  }
});
