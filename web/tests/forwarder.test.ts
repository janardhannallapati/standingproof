// @vitest-environment node
import { afterEach, describe, expect, it, vi } from "vitest";

import { GET, POST } from "@/app/api/[...path]/route";

const HOST = "localhost:3000";
const context = (...path: string[]) => ({ params: Promise.resolve({ path }) });

/** Records what would have been sent to the API, and answers 200. */
function upstream() {
  const sent: { url: string; init: RequestInit }[] = [];
  vi.stubGlobal("fetch", async (url: URL, init: RequestInit) => {
    sent.push({ url: String(url), init });
    return new Response('{"ok":true}', { status: 200, headers: { "content-type": "application/json" } });
  });
  return sent;
}

function post(headers: Record<string, string>, body = '{"decision":"approve","version":1}') {
  return new Request(`http://${HOST}/api/run-questions/q1/decision`, {
    method: "POST",
    headers: { host: HOST, ...headers },
    body,
  });
}

const OURS = { "content-type": "application/json", origin: `http://${HOST}`, "sec-fetch-site": "same-origin" };

afterEach(() => vi.unstubAllGlobals());

describe("the forwarder", () => {
  it("forwards our own page's decision to the API as JSON", async () => {
    const sent = upstream();

    const response = await POST(post(OURS), context("run-questions", "q1", "decision"));

    expect(response.status).toBe(200);
    expect(sent).toHaveLength(1);
    expect(sent[0]!.url).toBe("http://127.0.0.1:8000/run-questions/q1/decision");
    expect(sent[0]!.init.body).toBe('{"decision":"approve","version":1}');
  });

  it.each([
    ["a form posted from another site", { "content-type": "text/plain", origin: "https://evil.example", "sec-fetch-site": "cross-site" }],
    ["JSON from another site", { ...OURS, origin: "https://evil.example", "sec-fetch-site": "cross-site" }],
    ["another site that the browser calls same-site", { ...OURS, "sec-fetch-site": "same-site" }],
    ["a different port on this machine", { "content-type": "application/json", origin: "http://localhost:9999" }],
    ["a sandboxed page", { "content-type": "application/json", origin: "null" }],
  ])("refuses %s and sends nothing to the API", async (_name, headers) => {
    const sent = upstream();

    const response = await POST(post(headers), context("run-questions", "q1", "decision"));

    expect(response.status).toBe(403);
    expect(sent).toHaveLength(0);
  });

  it.each([
    ["a plain-text body", { ...OURS, "content-type": "text/plain" }],
    ["a form body", { ...OURS, "content-type": "application/x-www-form-urlencoded" }],
    ["no content type", { origin: `http://${HOST}` }],
  ])("refuses %s, which a form could send, even from our own origin", async (_name, headers) => {
    const sent = upstream();

    const response = await POST(post(headers), context("run-questions", "q1", "decision"));

    expect(response.status).toBe(415);
    expect(sent).toHaveLength(0);
  });

  it("accepts JSON with a charset, and a client that is not a browser", async () => {
    const sent = upstream();

    await POST(post({ ...OURS, "content-type": "application/json; charset=utf-8" }), context("runs", "r1", "approve-ready"));
    await POST(post({ "content-type": "application/json" }), context("runs", "r1", "approve-ready"));

    expect(sent).toHaveLength(2);
  });

  it("keeps the query string and cannot be steered to another path", async () => {
    const sent = upstream();
    const request = new Request(`http://${HOST}/api/runs/r1/questions?limit=5&after=2`, { headers: { host: HOST } });

    await GET(request, context("runs", "../../admin", "questions"));

    expect(sent[0]!.url).toBe("http://127.0.0.1:8000/runs/..%2F..%2Fadmin/questions?limit=5&after=2");
  });

  it("says so when the API cannot be reached", async () => {
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("fetch failed");
    });

    const response = await GET(new Request(`http://${HOST}/api/runs`, { headers: { host: HOST } }), context("runs"));

    expect(response.status).toBe(502);
    expect(await response.json()).toEqual({ detail: "The API could not be reached." });
  });
});

describe("the forwarder and a downloaded file", () => {
  const XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

  function upstreamFile(extra: Record<string, string> = {}) {
    vi.stubGlobal("fetch", async () =>
      new Response(new Uint8Array([80, 75, 3, 4]), {
        status: 200,
        headers: {
          "content-type": XLSX,
          "content-disposition": 'attachment; filename="acme-filled.xlsx"',
          "set-cookie": "session=from-the-api",
          ...extra,
        },
      }),
    );
  }

  const get = (path: string) => new Request(`http://${HOST}/api/${path}`, { headers: { host: HOST } });

  it("passes on the file's name and bytes for the download path", async () => {
    upstreamFile();

    const response = await GET(get("runs/r1/file/f1"), context("runs", "r1", "file", "f1"));

    expect(response.headers.get("content-type")).toBe(XLSX);
    expect(response.headers.get("content-disposition")).toBe('attachment; filename="acme-filled.xlsx"');
    expect(response.headers.get("x-content-type-options")).toBe("nosniff");
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(new Uint8Array(await response.arrayBuffer())).toEqual(new Uint8Array([80, 75, 3, 4]));
  });

  it.each([
    ["the preview", ["runs", "r1", "file", "preview"]],
    ["a question", ["run-questions", "q1"]],
    ["a longer path", ["runs", "r1", "file", "f1", "more"]],
  ])("passes on no such header for %s", async (_name, path) => {
    upstreamFile();

    const response = await GET(get(path.join("/")), context(...path));

    expect(response.headers.get("content-disposition")).toBeNull();
  });

  it("drops a file name that could break out of the header", async () => {
    upstreamFile({ "content-disposition": 'attachment; filename="a.xlsx"\r\nSet-Cookie: x=1' });

    const response = await GET(get("runs/r1/file/f1"), context("runs", "r1", "file", "f1"));

    expect(response.headers.get("content-disposition")).toBeNull();
  });
});
