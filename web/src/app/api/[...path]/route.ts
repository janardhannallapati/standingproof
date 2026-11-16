// Forwards the browser's calls to the API, so the page and its data share one origin.
// The API's address is read when a request arrives, not when the app is built, and it
// never reaches the browser.
const API_URL = () => process.env.API_URL ?? "http://127.0.0.1:8000";

const JSON_TYPE = "application/json";

function refuse(status: number, detail: string): Response {
  return Response.json({ detail }, { status });
}

/**
 * Why a request that changes something may not be forwarded, or null if it may.
 *
 * Another site can make a visitor's browser send a POST here (a hidden form is enough).
 * Two things tell such a request from our own page's:
 * - the browser says where it came from (`Sec-Fetch-Site`, `Origin`), and a page cannot
 *   set either;
 * - a form can only send three content types, and JSON is not one of them. Sending JSON
 *   from another site needs permission (CORS) that this app never gives.
 */
function crossSiteRefusal(request: Request): Response | null {
  const site = request.headers.get("sec-fetch-site");
  if (site !== null && site !== "same-origin") {
    return refuse(403, "A request from another site was refused.");
  }
  const origin = request.headers.get("origin");
  if (origin !== null) {
    let host: string | null = null;
    try {
      host = new URL(origin).host;
    } catch {
      // "null" (a sandboxed page, a file) or anything unparseable: not ours.
    }
    if (host === null || host !== request.headers.get("host")) {
      return refuse(403, "A request from another site was refused.");
    }
  }
  const type = (request.headers.get("content-type") ?? "").split(";")[0]?.trim().toLowerCase();
  if (type !== JSON_TYPE) {
    return refuse(415, "Send JSON (content-type: application/json).");
  }
  return null;
}

// Only what the API needs. No cookie or other header from the browser is passed on yet;
// when sign-in exists, this is where its credential will be attached.
async function forward(request: Request, path: string[], body?: string): Promise<Response> {
  const target = new URL(`${API_URL()}/${path.map(encodeURIComponent).join("/")}`);
  target.search = new URL(request.url).search;
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers: body === undefined ? undefined : { "content-type": JSON_TYPE },
      body,
      cache: "no-store",
    });
  } catch {
    return refuse(502, "The API could not be reached.");
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") ?? JSON_TYPE },
  });
}

type Context = { params: Promise<{ path: string[] }> };

export async function GET(request: Request, context: Context) {
  return forward(request, (await context.params).path);
}

export async function POST(request: Request, context: Context) {
  const refusal = crossSiteRefusal(request);
  if (refusal) return refusal;
  return forward(request, (await context.params).path, await request.text());
}
