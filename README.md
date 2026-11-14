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

## Upload a questionnaire

With the stack running, upload a customer's `.xlsx` file. Each question becomes a row that
records the sheet and cell it came from. The original file is stored unchanged.

```sh
curl -F "file=@questionnaire.xlsx" localhost:8000/questionnaires
curl localhost:8000/questionnaires/<id>/questions
```

Columns are found by their headings ("Question", "Requirement", "Response" and similar). When
none is recognised, the response has status `needs_mapping` and a preview of each sheet, and you
choose the columns:

```sh
curl -X PUT localhost:8000/questionnaires/<id>/mapping \
  -H "content-type: application/json" \
  -d '{"sheet": "Sheet1", "header_row": 3, "question_column": "B", "answer_column": "D"}'
```

Only plain `.xlsx` files up to 5 MB are accepted; macro-enabled workbooks are refused. Uploading
the same file twice returns the first result. The interactive API reference is at
`localhost:8000/docs`.

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
