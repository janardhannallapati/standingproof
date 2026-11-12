# Contributing

1. Open an issue describing the change before large work.
2. `uv sync`, then `uv run pytest -v` (Docker must be running).
3. Write a failing test first, then the change.
4. `uv run ruff check . && uv run ruff format . && uv run mypy`
5. Open a pull request. CI must be green.

By contributing you agree that your work is licensed under Apache-2.0.
