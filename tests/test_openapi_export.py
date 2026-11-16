"""The OpenAPI file the web app's types are generated from is the API's own, today."""

from scripts.export_openapi import TARGET, document


def test_the_committed_openapi_document_is_current() -> None:
    assert TARGET.read_text() == document(), (
        "web/openapi.json is out of date. Run: uv run python scripts/export_openapi.py "
        "and then, in web/: pnpm gen:api"
    )
