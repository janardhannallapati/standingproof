import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import AxeBuilder from "@axe-core/playwright";
import { type APIRequestContext, expect, request, test } from "@playwright/test";

const API = process.env.E2E_API_URL ?? "http://127.0.0.1:8000";
const SHEET = fileURLToPath(new URL("./fixtures/dropdown-comments.xlsx", import.meta.url));
const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

// A sheet whose Answer column is a Yes / No / N/A dropdown with a Comments column beside
// it. One approved answer begins with "Yes."; the other does not begin with a value.
const MFA = "Dropdown test: is multi-factor authentication enforced?";
const ENCRYPTION = "Dropdown test: how is customer data encrypted?";

let api: APIRequestContext;
let runId: string;

test.beforeAll(async () => {
  api = await request.newContext({ baseURL: API });
  for (const [question, answer] of [
    [MFA, "Yes. Multi-factor authentication is enforced for all staff."],
    [ENCRYPTION, "Customer data is encrypted at rest with AES-256."],
  ]) {
    const added = await api.post("/library/answers", {
      data: { question, answer, owner: "security", expires_on: "2099-01-01" },
    });
    expect(added.status()).toBe(201);
  }
  const uploaded = await api.post("/questionnaires", {
    multipart: { file: { name: "dropdown-comments.xlsx", mimeType: XLSX, buffer: readFileSync(SHEET) } },
  });
  expect([200, 201]).toContain(uploaded.status());
  const started = await api.post(`/questionnaires/${(await uploaded.json()).id}/runs`);
  expect([200, 202]).toContain(started.status());
  runId = (await started.json()).id;
  await expect
    .poll(async () => (await (await api.get(`/runs/${runId}`)).json()).status, { timeout: 45_000 })
    .toBe("finished");
  const approved = await api.post(`/runs/${runId}/approve-ready`, { data: { count: 2 } });
  expect(approved.status()).toBe(200);
});

test.afterAll(async () => {
  await api.dispose();
});

test("a dropdown gets its value, the comments cell the sentence, and the file is recorded as sent", async ({ page }) => {
  await page.goto(`/runs/${runId}/file`);
  await expect(page.getByText(/^2 answers will be written, in 3 cells\. 1 cell is left empty\./)).toBeVisible();
  const rows = page.getByRole("table", { name: "What is written" }).getByRole("row");
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(1)).toContainText("Security!C2");
  await expect(rows.nth(1)).toContainText("the dropdown's value, taken from the first word of the approved answer");
  await expect(rows.nth(2)).toContainText("Security!D2");
  await expect(page.getByRole("list", { name: "Left empty" }).getByRole("listitem")).toContainText("Security!C3");
  await expect(page.getByLabel("Sent to")).toHaveCount(0);

  const waiting = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download filled file" }).click();
  const bytes = readFileSync(await (await waiting).path());
  const latest = (await (await api.get(`/runs/${runId}/file/preview`)).json()).latest;
  expect(createHash("sha256").update(bytes).digest("hex")).toBe(latest.sha256);

  await page.getByLabel("Sent to").fill("Acme Corp");
  await page.getByRole("button", { name: "Record as sent" }).click();
  await expect(page.getByRole("list", { name: "Recorded as sent" }).getByRole("listitem")).toContainText(
    "to Acme Corp",
  );
  const sent = (await (await api.get(`/runs/${runId}/file/preview`)).json()).sent;
  expect(sent.map((s: { customer: string; answers: number }) => [s.customer, s.answers])).toEqual([["Acme Corp", 2]]);
});

for (const scheme of ["light", "dark"] as const) {
  test(`the file page with a sent record has no accessibility violations in ${scheme}`, async ({ page }) => {
    await page.emulateMedia({ colorScheme: scheme });
    await page.goto(`/runs/${runId}/file`);
    await expect(page.getByRole("list", { name: "Recorded as sent" })).toBeVisible();

    const results = await new AxeBuilder({ page }).analyze();

    const found = results.violations.map((v) => `${v.id}: ${v.help} -> ${v.nodes.map((n) => n.target.join(" ")).join(" | ")}`);
    expect(found).toEqual([]);
  });
}
