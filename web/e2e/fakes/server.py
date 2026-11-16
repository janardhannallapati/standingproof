"""Stand-ins for the model provider and for GitHub, for the browser tests only.

Runs as the `fakes` service of compose.e2e.yaml. Nothing in the product knows it is not
talking to the real thing: the worker's SDK is pointed here by ANTHROPIC_BASE_URL and the
check client by SP_GITHUB_API_URL. Standard library only.

  POST /anthropic/v1/messages      a scripted model (see `model_reply`)
  GET  /github/repos/e2e-org/app   the repository, and its branch protection under it
  POST /control/protection         {"reviews": 2} or {"reviews": null}: what the test flips
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

REPO = "e2e-org/app"
# Only a question that carries this marker is drafted. Every other request is refused the
# way a bad key is, so the other browser tests see "the model could not be called".
MARKER = "Drift test:"

state: dict[str, Any] = {"reviews": 2}
lock = threading.Lock()


def _message(content: list[dict[str, Any]], stop_reason: str) -> dict[str, Any]:
    return {
        "id": "msg_e2e",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def _tool_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """What the product's own check tool returned, if the conversation has got that far."""
    for message in reversed(messages):
        blocks = message.get("content")
        for block in blocks if isinstance(blocks, list) else []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                content = block.get("content")
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content)
                try:
                    return dict(json.loads(content))
                except (TypeError, ValueError):
                    return None
    return None


def model_reply(body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Look at branch protection once, then answer with exactly what the look said."""
    messages = body.get("messages", [])
    if MARKER not in json.dumps(messages):
        return 401, {
            "type": "error",
            "error": {"type": "authentication_error", "message": "not a real key"},
        }
    seen = _tool_result(messages)
    if seen is None:
        call = {
            "type": "tool_use",
            "id": "toolu_e2e",
            "name": "check_live_setting",
            "input": {"check": "github.branch_protection", "scope": REPO},
        }
        return 200, _message([call], "tool_use")
    draft = {
        "status": "answered",
        "answer": seen["statement"],
        "citations": [seen["id"]],
        "note": "",
    }
    return 200, _message([{"type": "text", "text": json.dumps(draft)}], "end_turn")


def github_reply(path: str) -> tuple[int, dict[str, Any]]:
    with lock:
        reviews = state["reviews"]
    if path == f"/repos/{REPO}":
        return 200, {"default_branch": "main"}
    if path == f"/repos/{REPO}/branches/main/protection" and reviews:
        return 200, {
            "required_pull_request_reviews": {"required_approving_review_count": reviews},
            "enforce_admins": {"enabled": True},
        }
    return 404, {"message": "Not Found"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict[str, Any]) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self) -> dict[str, Any]:
        length = int(self.headers.get("content-length") or 0)
        return dict(json.loads(self.rfile.read(length) or b"{}"))

    def do_GET(self) -> None:  # noqa: N802 - the base class's name
        path = self.path.split("?")[0]
        if path == "/healthz":
            self._send(200, {"ok": True})
        elif path.startswith("/github/"):
            self._send(*github_reply(path[len("/github") :]))
        else:
            self._send(404, {"message": "Not Found"})

    def do_POST(self) -> None:  # noqa: N802 - the base class's name
        path = self.path.split("?")[0]
        if path.startswith("/anthropic/") and path.endswith("/messages"):
            self._send(*model_reply(self._json()))
        elif path == "/control/protection":
            with lock:
                state["reviews"] = self._json().get("reviews")
            self._send(200, dict(state))
        else:
            self._send(404, {"message": "Not Found"})

    def log_message(self, *_: object) -> None:
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 9000), Handler).serve_forever()  # noqa: S104 - in a container
