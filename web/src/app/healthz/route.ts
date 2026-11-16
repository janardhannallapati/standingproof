// Liveness of the web server itself. It does not ask the API.
export function GET() {
  return Response.json({ status: "ok" });
}
