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

## Load your library and search it

The library is what the agent may answer from: answers your company has already approved, and
your policy documents.

```sh
curl -X POST localhost:8000/library/answers -H "content-type: application/json" -d '{
  "question": "Is multi-factor authentication required for employees?",
  "answer": "Yes. Multi-factor authentication is required for all employee accounts.",
  "owner": "security-lead@example.com",
  "expires_on": "2027-06-30",
  "control_id": "CC6.1"
}'
curl -F "file=@access-control-policy.md" localhost:8000/library/policies
curl --get localhost:8000/library/search --data-urlencode "q=Do you enforce 2FA for staff?"
```

Search matches by wording and by meaning, so the question above finds the answer although it
shares no word with it. Each result says how it was found (`keyword`, `meaning`, or an exact
`control_id`) and carries its owner and expiry date. An approved answer past its expiry date is
kept and listed, and is never returned by search. Policies are Markdown or plain text up to
1 MB; they are split by heading, and a result names the heading it came from.

Search returns the closest entries it has. It does not decide that a question has no approved
source: on our test set, questions with no source and questions with one had nearest matches
at the same distances. That decision belongs to the answer check, not to retrieval.

### Embedding model

Text is turned into vectors by `BAAI/bge-small-en-v1.5`, which runs inside the process. No
key is needed and no library text leaves the machine. The runtime that executes the model
(ONNX Runtime) ships with usage telemetry switched on; Standingproof switches it off before
the runtime loads, and a test fails if the runtime records anything. Vectors are stored per model, so another
model can be tried beside it:

```sh
export SP_EMBEDDING_MODEL=voyage-4-lite VOYAGE_API_KEY=<your key>
docker compose up --build -d --wait
docker compose exec api python -m standingproof.library reembed
```

With a Voyage model selected, library text and search queries are sent to Voyage AI. Unset both
variables to return to the local model; its vectors are still there.

Retrieval is measured, not assumed: `uv run pytest -m slow -s tests/eval` runs 33 questions
against a sample library with the real model and prints, for keyword, meaning and combined
search, how often the right source is in the top five.

## Check a live setting

An approved answer says what the policy is. A check looks at what the system does. Standingproof
can read seven settings from GitHub and AWS, store what it saw with the time, and give that to
the agent as a source it can cite.

```sh
curl -X POST localhost:8000/connections -H "content-type: application/json" \
  -d '{"system": "github", "scope": "your-org"}'
curl -X POST localhost:8000/checks/github.branch_protection/run \
  -H "content-type: application/json" -d '{"scope": "your-org/your-repo"}'
```

```json
{
  "status": "observed",
  "statement": "Branch main of your-org/your-repo is protected: a change needs 1 approving review before it can be merged, and the rule does not apply to administrators.",
  "facts": {"default_branch": "main", "protected": true, "required_approving_reviews": 1, "applies_to_administrators": false},
  "observed_at": "2026-11-16T10:02:11Z"
}
```

`GET /checks` lists them: two-factor sign-in for a GitHub organisation, branch protection and
secret scanning for a repository, and for an AWS account root MFA, the password policy, the
account-wide block on public S3 buckets and default disk encryption.

- **Read-only, three times over.** The credentials you supply should be read-only
  ([SECURITY.md](SECURITY.md) lists the exact permissions). The clients can send only the
  operations on an allow-list in `checks/registry.py`. A test fails if a check uses another.
- **Default deny.** A check runs only on an organisation or account that is connected, and one
  can be connected only if the operator has listed it in `SP_GITHUB_ORGS` or `SP_AWS_ACCOUNTS`.
  The credentials belong to the deployment, so a caller cannot grant itself a scope, and a
  scope is held by one tenant. Every attempt is stored, including refused ones.
- **Nothing is guessed.** If the system refuses, is unreachable or does not show the setting,
  the result says "not observed" and why, and offers no fact.
- **Only what was needed is kept.** A few named fields of the reply are stored, never the
  whole reply.
- An observation is reused for 15 minutes, then looked at again.

To see every check against your own accounts without storing anything:

```sh
uv run python scripts/try_checks.py --github-org your-org --github-repo your-org/your-repo \
  --aws-account 123456789012
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
