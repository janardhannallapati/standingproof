import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, expect, request, test } from "@playwright/test";

// Needs the stack started with compose.e2e.yaml on top of compose.yaml, like drift.spec.ts.
// The day's spend ceiling is set the way an operator sets it: with the command, inside the
// api container. Nothing is written to the database by the test itself.
const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
const REPO = fileURLToPath(new URL("../..", import.meta.url));
const SHEET = fileURLToPath(new URL("./fixtures/drift-question.xlsx", import.meta.url));
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
const QUESTION = "Drift test: do code changes need a second person's review before they are merged?";
const AT_THE_CEILING = "Stopped: today's spend ceiling was reached";

let api: APIRequestContext;
let runId: string;
let questionId: string;

/** `python -m standingproof.limits ...` as an operator would run it. */
function limits(...words: string[]): string {
  // This command goes to whichever stack Compose resolves from the environment. With the
  // tests pointed at a second stack (E2E_API_URL) and no COMPOSE_PROJECT_NAME, that would be
  // the default one: somebody's own running stack, whose ceiling this test sets to zero.
  if (process.env.E2E_API_URL && !process.env.COMPOSE_PROJECT_NAME) {
    throw new Error("E2E_API_URL is set: set COMPOSE_PROJECT_NAME to that stack's project as well");
  }
  return execFileSync(
    "docker",
    ["compose", "-f", "compose.yaml", "-f", "compose.e2e.yaml", "exec", "-T", "api", "python", "-m", "standingproof.limits", ...words],
    { cwd: REPO, encoding: "utf8" },
  );
}

test.beforeAll(async () => {
  api = await request.newContext({ baseURL: API });
  // The first request is what creates the tenant on a fresh stack.
  expect((await api.get("/runs")).status()).toBe(200);
  // Nothing may be spent today: the next model call is refused before it is made.
  limits("daily", "--by", "e2e", "--usd", "0", "--tenant", "default");

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
});

test.afterAll(async () => {
  // Back to the ceiling in the settings, whatever happened above.
  limits("daily", "--by", "e2e", "--default", "--tenant", "default");
  await api.dispose();
});

test("a question stopped at the day's ceiling says so in words and waits for a person", async ({ page }) => {
  const stopped = await (await api.get(`/run-questions/${questionId}`)).json();
  // No draft, no cost: the model was never called.
  expect([stopped.status, stopped.reason, stopped.answer]).toEqual(["needs_review", "daily_limit", null]);

  await page.goto(`/runs/${runId}?question=${questionId}`);
  await expect(page.getByRole("heading", { level: 1, name: QUESTION })).toBeVisible();
  // The reason in words, in the list and beside the question. Never the code.
  await expect(page.getByText(AT_THE_CEILING).first()).toBeVisible();
  await expect(page.getByText("daily_limit")).toHaveCount(0);
  await expect(page.getByText("daily limit", { exact: true })).toHaveCount(0);

  const results = await new AxeBuilder({ page }).analyze();
  expect(results.violations.map((v) => `${v.id}: ${v.help}`)).toEqual([]);

  // The operator sees the same thing from the command.
  expect(limits("show")).toMatch(/tenant default: running, \$\d+\.\d\d of \$0\.00 today/);
});
