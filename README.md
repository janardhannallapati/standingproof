# Standingproof

Answers security questionnaires from approved sources, verifies each answer against live system
settings where it can, and reopens answers already sent when those settings change.

> **Status:** early development. Not ready for use.

## Run it

Requires Docker.

```sh
docker compose up --build
curl localhost:8000/readyz
```

## Try the agent on one question

Needs an Anthropic API key in `.env` (see `.env.example`). Each run costs a few cents.

```sh
uv run python -m standingproof.agent "Is multi-factor authentication enforced for all employees?"
```

The agent searches a small sample library of approved answers, replies only from what it finds,
and reports "no approved source" when nothing supports an answer.

## Develop

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync
uv run pytest -v
```

See [CONTRIBUTING.md](CONTRIBUTING.md). To report a vulnerability, see [SECURITY.md](SECURITY.md).

## Licence

[Apache-2.0](LICENSE)
