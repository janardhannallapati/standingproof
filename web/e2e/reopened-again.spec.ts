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
 * The same start as drift.spec.ts, with nothing written to the database by the test: an
 * approved and sent answer rests on a look at (fake) GitHub, branch protection is switched
 * off, and the watcher reopens the answer. The test then goes on from there.
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

test("a redraft that cites the first look again is reopened with its own record of what changed", async ({ page }) => {
  await page.goto(`/runs/${runId}?question=${questionId}`);
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expect(
    page.getByText("Reopened: a live setting this answer rests on has changed since it was approved."),
  ).toBeVisible();

  // The reviewer sends it back. The scripted model answers from the look it already has
  // in its saved conversation, the one made before protection was switched off.
  await page.getByRole("button", { name: /Ask for a redraft/ }).click();
  await page.getByLabel("What should the new draft do differently?").fill("Shorter, please.");
  await page.getByRole("button", { name: "Send back" }).click();

  // The worker saves the new draft; the newest look is compared with the one it cites; the
  // same change reopens the answer a second time.
  await expect
    .poll(
      async () => {
        const now = await question();
        return [now.review, now.redrafts, now.reopening !== null];
      },
      { timeout: 45_000 },
    )
    .toEqual(["pending", 1, true]);
  const again = await question();
  expect(again.reopening.was_review).toBeNull();
  expect(again.final_answer).toBeNull();
  expect(again.answer).toContain("needs 2 approving reviews");

  await page.reload();
  await expect(page.getByText("Live", { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { level: 1, name: QUESTION })).toBeVisible();
  // What the reviewer is told now: not a green tick, and what the setting reads today.
  await expect(
    page.getByText("Reopened: a live setting this answer rests on has changed since it was drafted."),
  ).toBeVisible();
  await expect(page.getByText(/^Drafted from 1 cited source/)).toHaveCount(0);
  const table = page.getByRole("table", { name: SETTING });
  await expect(table.getByRole("columnheader", { name: /When drafted/ })).toBeVisible();
  await expect(table.getByRole("row", { name: /Approving reviews needed/ })).toContainText("changed: 0");
  await expect(page.getByText(NOT_PROTECTED).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "Proposed answer" })).toBeVisible();

  // Nothing on the screen can be refused: no "Approve", and its key does nothing.
  await expect(page.getByRole("button", { name: "Approve", exact: true })).toHaveCount(0);
  await page.keyboard.press("a");
  expect((await question()).review).toBe("pending");
  await expect(page.getByRole("button", { name: "Keep this answer" })).toBeVisible();

  for (const scheme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: scheme });
    expect(await violations(page), `reopened again, ${scheme}`).toEqual([]);
  }

  // The reviewer writes what is true now.
  await page.getByRole("button", { name: /^Edit/ }).click();
  await page.getByLabel("Answer to approve").fill("No. Branch protection on main is currently switched off.");
  await page.getByRole("button", { name: "Save and approve" }).click();
  await expect
    .poll(async () => {
      const after = await question();
      return [after.review, after.reopening];
    })
    .toEqual(["edited", null]);
});
