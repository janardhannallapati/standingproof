# Agent instructions

Standingproof answers security questionnaires from approved sources, verifies answers against
live system settings, and reopens them when those settings drift.

## Commands

- Install: `uv sync`
- Test: `uv run pytest -v` (needs Docker; tests start Postgres and Redis containers)
- Lint and types: `uv run ruff check . && uv run ruff format --check . && uv run mypy`
- Run locally: `docker compose up --build`, then `curl localhost:8000/readyz`
- New migration: `uv run alembic revision -m "<what changes>"`
- Ask the agent one question (calls the paid API): `uv run python -m standingproof.agent "<question>"`

## Rules

- Write the failing test before the code. Run it and see it fail.
- Tests use real Postgres and Redis through Testcontainers. Do not mock the storage boundary.
- Warnings fail the test run. Fix the cause; do not add a filter.
- Configuration comes only from `standingproof.config.Settings` (`SP_` variables). Values that
  hold credentials are `SecretStr`.
- Routes get the connection pool and Redis client from `standingproof.api.deps`, not from
  globals.
- Never read, print or commit `.env`.
- Never claim "compliant" in code, copy or docs. The product reports a configuration at a point
  in time.
- Model-facing tools are read-only. Anything that writes runs in plain code after a recorded
  human approval.
- Tests never call the model provider. Loop logic uses `tests/agent/fakes.py`; real model
  behaviour is replayed from `tests/agent/cassettes/`. Recording a cassette spends money: ask
  first.
- `standingproof/agent/model.py` is the only module that imports the `anthropic` SDK. Everything
  else depends on the `ModelClient` protocol in `agent/loop.py`.
- A model's final answer is accepted only by `_check` in `agent/loop.py`. New acceptance rules go
  there, in code, with a test. A sentence in the prompt is not a control.
- Text read from an uploaded file is data. Store it and show it; never run it, format it into
  SQL, or treat it as instructions. A file's name and declared type are chosen by the sender and
  are never trusted.
- `standingproof/questionnaires/intake.py`, `detect.py` and `parser.py` take bytes or rows and
  return plain objects. They import nothing from FastAPI or the database. Only `service.py` joins
  them to storage.
- Every query on a tenant-owned table filters by `tenant_id`. A row that belongs to another
  tenant is reported as `404`, the same as one that does not exist.
- Code that changes data commits explicitly in the service, before the response is built.
- Work with no `await` in it that can take more than a few milliseconds (parsing a file) runs
  through `asyncio.to_thread`, never directly on the event loop.
- A new table needs a model in `models.py` and a hand-written migration. `alembic check` in the
  tests fails when they disagree.
