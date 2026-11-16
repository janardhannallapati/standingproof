// The API, typed from its own OpenAPI document (src/lib/api-types.ts is generated:
// `pnpm gen:api`). Nothing here knows a field the API does not declare.
import type { components } from "./api-types";

type Schemas = components["schemas"];
export type Run = Schemas["RunOut"];
export type RunListItem = Schemas["RunListItem"];
export type RunQuestion = Schemas["RunQuestionOut"];
export type ReviewQuestion = Schemas["ReviewQuestionOut"];
export type Source = Schemas["SourceOut"];
export type Check = Schemas["DraftCheckOut"];
export type Finding = Schemas["FindingOut"];
export type DecisionBody = Schemas["DecisionIn"];
export type Decision = DecisionBody["decision"];
export type FilePreview = Schemas["FilePreviewOut"];
export type FilledFile = Schemas["FilledFileOut"];
export type SentRecord = Schemas["SentOut"];

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

// In the browser every call goes to this app's own /api, which forwards it. On the
// server the API is called directly, at an address the browser never sees.
const BROWSER_BASE = "/api";

function base(): string {
  if (typeof window !== "undefined") return BROWSER_BASE;
  return process.env.API_URL ?? "http://127.0.0.1:8000";
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${base()}${path}`, {
    ...init,
    cache: "no-store",
    headers: init?.body ? { "content-type": "application/json" } : undefined,
  });
  if (!response.ok) {
    let message = `The server answered ${response.status}.`;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body && typeof body.detail === "string") {
        message = body.detail;
      }
    } catch {
      // Not JSON: keep the plain message.
    }
    throw new ApiError(response.status, message);
  }
  return (await response.json()) as T;
}

export function listRuns(): Promise<{ items: RunListItem[] }> {
  return call("/runs");
}

export function getRun(runId: string): Promise<Run> {
  return call(`/runs/${runId}`);
}

export async function getRunQuestions(runId: string): Promise<RunQuestion[]> {
  const all: RunQuestion[] = [];
  let after: number | null = 0;
  while (after !== null) {
    const page: { items: RunQuestion[]; next_after: number | null } = await call(
      `/runs/${runId}/questions?limit=500&after=${after}`,
    );
    all.push(...page.items);
    after = page.next_after;
  }
  return all;
}

export function getQuestion(itemId: string): Promise<ReviewQuestion> {
  return call(`/run-questions/${itemId}`);
}

export function decide(itemId: string, body: DecisionBody): Promise<ReviewQuestion> {
  return call(`/run-questions/${itemId}/decision`, { method: "POST", body: JSON.stringify(body) });
}

export function approveReady(runId: string, count: number): Promise<{ approved: number }> {
  return call(`/runs/${runId}/approve-ready`, { method: "POST", body: JSON.stringify({ count }) });
}

export function getFilePreview(runId: string): Promise<FilePreview> {
  return call(`/runs/${runId}/file/preview`);
}

/** Makes the file for the decisions as they are, or finds the one already made. */
export function makeFile(runId: string): Promise<FilledFile> {
  // The body is empty JSON: this app's forwarder accepts a POST only as JSON.
  return call(`/runs/${runId}/file`, { method: "POST", body: "{}" });
}

/** Where the browser fetches the file from: this app's own origin. */
export function fileUrl(runId: string, fileId: string): string {
  return `/api/runs/${runId}/file/${fileId}`;
}

/** Records that a reviewer sent this file to a customer. Safe to repeat. */
export function recordSent(runId: string, fileId: string, customer: string): Promise<SentRecord> {
  return call(`/runs/${runId}/file/${fileId}/sent`, {
    method: "POST",
    body: JSON.stringify({ customer }),
  });
}
