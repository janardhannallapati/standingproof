// Forwards the browser's calls to the API, so the page and its data share one origin.
// The API's address is read when a request arrives, not when the app is built, and it
// never reaches the browser.
const API_URL = () => process.env.API_URL ?? "http://127.0.0.1:8000";

// Only what the API needs. No cookie or other header from the browser is passed on yet;
// when sign-in exists, this is where its credential will be attached.
async function forward(request: Request, path: string[]): Promise<Response> {
  const target = new URL(`${API_URL()}/${path.map(encodeURIComponent).join("/")}`);
  target.search = new URL(request.url).search;
  const hasBody = request.method !== "GET" && request.method !== "HEAD";
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers: hasBody ? { "content-type": "application/json" } : undefined,
      body: hasBody ? await request.text() : undefined,
      cache: "no-store",
    });
  } catch {
    return Response.json({ detail: "The API could not be reached." }, { status: 502 });
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? "application/json" },
  });
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: Request, context: Context) {
  return forward(request, (await context.params).path);
}

export async function POST(request: Request, context: Context) {
  return forward(request, (await context.params).path);
}
