"""Write the API's OpenAPI document to web/openapi.json.

    uv run python scripts/export_openapi.py

The web app's TypeScript types are generated from this file, so the screen cannot disagree
with the API about a field. A test fails when the committed file is out of date.
"""

import json
from pathlib import Path

from standingproof.app import create_app
from standingproof.checks.service import CheckBackends
from standingproof.config import Settings

TARGET = Path(__file__).parents[1] / "web" / "openapi.json"


class _NoEmbedder:
    """Building the document needs no model; this keeps the script from loading one."""

    async def aclose(self) -> None:
        return None


def document() -> str:
    app = create_app(
        Settings(_env_file=None),
        embedder=_NoEmbedder(),  # type: ignore[arg-type]
        backends=CheckBackends(github=None, aws=None),
    )
    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


if __name__ == "__main__":
    TARGET.write_text(document())
    print(f"wrote {TARGET}")
