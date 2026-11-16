import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Inbox } from "@/components/Inbox";
import type { ReviewQuestion, RunQuestion } from "@/lib/api";

import { detailed, listed, run } from "./fixtures";

/** A stand-in for the browser's WebSocket that the test drives by hand. */
class FakeSocket {
  static all: FakeSocket[] = [];
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  closed = false;
  constructor(readonly url: string) {
    FakeSocket.all.push(this);
  }
  say(type: string) {
    this.onmessage?.({ data: JSON.stringify({ type }) });
  }
  loseConnection() {
    this.onclose?.();
  }
  close() {
    this.closed = true;
  }
}

type Call = { url: string; method: string; body: unknown };

const asListed = (q: ReviewQuestion): RunQuestion =>
  listed({
    id: q.id,
    position: q.position,
    question: q.question,
    status: q.status,
    path: q.path,
    reason: q.reason,
    answer: q.answer,
    review: q.review,
    version: q.version,
  });

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

/** The API, as far as the inbox uses it, answering from in-memory rows. */
function fakeApi(rows: ReviewQuestion[]) {
  const calls: Call[] = [];
  let refusal: { status: number; detail: string } | null = null;

  vi.stubGlobal("fetch", async (input: string, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    const body = init?.body ? JSON.parse(String(init.body)) : null;
    calls.push({ url, method, body });
    if (refusal && method === "POST") {
      const { status, detail } = refusal;
      refusal = null;
      return json({ detail }, status);
    }
    const decision = url.match(/\/run-questions\/([^/]+)\/decision$/);
    if (decision && method === "POST") {
      const row = rows.find((q) => q.id === decision[1])!;
      row.review = body.decision === "approve" ? "approved" : body.decision === "edit" ? "edited" : "rejected";
      row.final_answer = body.decision === "edit" ? body.answer : body.decision === "approve" ? row.answer : null;
      row.version += 1;
      return json({ ...row, warnings: body.answer?.includes("certified") ? ["certified"] : [] });
    }
    if (url.endsWith("/approve-ready")) {
      const ready = rows.filter((q) => q.status === "ready" && q.review === "pending");
      for (const q of ready) {
        q.review = "approved";
        q.version += 1;
      }
      return json({ approved: ready.length });
    }
    const one = url.match(/\/run-questions\/([^/?]+)$/);
    if (one) return json(rows.find((q) => q.id === one[1]));
    if (url.includes("/questions")) return json({ items: rows.map(asListed), next_after: null });
    return json(run());
  });

  return {
    calls,
    posts: () => calls.filter((c) => c.method === "POST"),
    refuse: (status: number, detail: string) => {
      refusal = { status, detail };
    },
  };
}

function rows(): ReviewQuestion[] {
  return [
    detailed({ id: "ready-1", position: 1, question: "Is MFA enforced?" }),
    detailed({ id: "ready-2", position: 2, question: "Is data encrypted at rest?", answer: "Yes, AES-256." }),
    detailed({
      id: "needs-3",
      position: 3,
      question: "Do you carry cyber insurance?",
      status: "needs_review",
      reason: "no_approved_source",
      answer: null,
      path: null,
      sources: [],
    }),
  ];
}

function open(data: ReviewQuestion[]) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <Inbox
        run={run()}
        questions={data.map(asListed)}
        filename="acme.xlsx"
        eventsUrl="ws://api.test/runs/run-1/events"
      />
    </QueryClientProvider>,
  );
}

const heading = (name: string) => screen.findByRole("heading", { level: 1, name });

beforeEach(() => {
  FakeSocket.all = [];
  vi.stubGlobal("WebSocket", FakeSocket);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the inbox", () => {
  it("opens on the question that needs a person, with its reason", async () => {
    fakeApi(rows());
    open(rows());

    expect(await heading("Do you carry cyber insurance?")).toBeInTheDocument();
    expect(screen.getAllByText(/No approved source/).length).toBeGreaterThan(0);
  });

  it("approves with the A key, names the version it saw, and moves to the next waiting question", async () => {
    const data = rows();
    const api = fakeApi(data);
    open(data);
    await userEvent.click(await screen.findByRole("button", { name: /Is MFA enforced/ }));
    await heading("Is MFA enforced?");

    await userEvent.keyboard("a");

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]!.url).toBe("/api/run-questions/ready-1/decision");
    expect(api.posts()[0]!.body).toEqual({ decision: "approve", version: 1, answer: null, comment: null });
    expect(await heading("Is data encrypted at rest?")).toBeInTheDocument();
  });

  it("types into the answer field instead of deciding", async () => {
    const data = rows();
    const api = fakeApi(data);
    open(data);
    await userEvent.click(await screen.findByRole("button", { name: /Is MFA enforced/ }));
    await heading("Is MFA enforced?");

    await userEvent.keyboard("e");
    const field = await screen.findByLabelText("Answer to approve");
    await userEvent.clear(field);
    await userEvent.type(field, "a read dear");

    expect(field).toHaveValue("a read dear");
    expect(api.posts()).toHaveLength(0);
  });

  it("does not approve with A when there is nothing to approve", async () => {
    const data = rows();
    const api = fakeApi(data);
    open(data);
    await heading("Do you carry cyber insurance?");

    await userEvent.keyboard("a");

    expect(api.posts()).toHaveLength(0);
  });

  it("moves between questions with J and K", async () => {
    fakeApi(rows());
    open(rows());
    await heading("Do you carry cyber insurance?");

    await userEvent.keyboard("j");
    expect(await heading("Is MFA enforced?")).toBeInTheDocument();
    await userEvent.keyboard("k");
    expect(await heading("Do you carry cyber insurance?")).toBeInTheDocument();
  });

  it("says so, and reloads, when someone else decided first", async () => {
    const data = rows();
    const api = fakeApi(data);
    open(data);
    await userEvent.click(await screen.findByRole("button", { name: /Is MFA enforced/ }));
    await heading("Is MFA enforced?");
    api.refuse(409, "this question has changed since you opened it");

    await userEvent.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Not saved: this question has changed since you opened it",
    );
  });

  it("stays on an edited answer to show a warning about its wording", async () => {
    const data = rows();
    fakeApi(data);
    open(data);
    await userEvent.click(await screen.findByRole("button", { name: /Is MFA enforced/ }));
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const field = await screen.findByLabelText("Answer to approve");
    await userEvent.clear(field);
    await userEvent.type(field, "Yes. We are certified.");

    await userEvent.click(screen.getByRole("button", { name: "Save and approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("no cited source uses the word “certified”");
    expect(screen.getByRole("heading", { level: 1, name: "Is MFA enforced?" })).toBeInTheDocument();
  });

  it("asks twice before approving every ready answer, and sends the count it showed", async () => {
    const data = rows();
    const api = fakeApi(data);
    open(data);

    await userEvent.click(await screen.findByRole("button", { name: "Approve all 2" }));
    expect(api.posts()).toHaveLength(0);
    await userEvent.click(screen.getByRole("button", { name: "Approve 2 unopened" }));

    await waitFor(() => expect(api.posts()).toHaveLength(1));
    expect(api.posts()[0]!.url).toBe("/api/runs/run-1/approve-ready");
    expect(api.posts()[0]!.body).toEqual({ count: 2 });
  });
});

describe("staying current", () => {
  it("connects to the run's events and refetches when told something changed", async () => {
    const api = fakeApi(rows());
    open(rows());
    await screen.findByRole("heading", { level: 1 });
    const socket = FakeSocket.all[0]!;
    expect(socket.url).toBe("ws://api.test/runs/run-1/events");

    act(() => socket.say("ready"));
    expect(await screen.findByText("Live")).toBeInTheDocument();
    const before = api.calls.length;
    act(() => socket.say("changed"));

    await waitFor(() => expect(api.calls.length).toBeGreaterThan(before));
  });

  it("says it is not live when the connection is lost, and tries again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fakeApi(rows());
    open(rows());
    await screen.findByRole("heading", { level: 1 });
    act(() => FakeSocket.all[0]!.say("ready"));
    await screen.findByText("Live");

    act(() => FakeSocket.all[0]!.loseConnection());
    expect(await screen.findByText("Not live: checking every 5 seconds")).toBeInTheDocument();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1100);
    });

    expect(FakeSocket.all).toHaveLength(2);
  });

  it("ignores a message that is not what it expects", async () => {
    fakeApi(rows());
    open(rows());
    await screen.findByRole("heading", { level: 1 });

    act(() => FakeSocket.all[0]!.onmessage?.({ data: "not json" }));
    act(() => FakeSocket.all[0]!.say("something_else"));

    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  });
});
