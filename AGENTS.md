# Agent instructions

Standingproof answers security questionnaires from approved sources, verifies answers against
live system settings, and reopens them when those settings drift.

## Commands

- Install: `uv sync`
- Test: `uv run pytest -v` (needs Docker; tests start Postgres and Redis containers)
- Retrieval scores with the real embedding model: `uv run pytest -m slow -s tests/eval`
- Lint and types: `uv run ruff check . && uv run ruff format --check . && uv run mypy`
- Run locally: `docker compose up --build`, then `curl localhost:8000/readyz`
- Slow tests (real embedding model; a worker process killed mid-run): `uv run pytest -m slow`
- New migration: `uv run alembic revision -m "<what changes>"`
- Web app (`web/`): `pnpm install --frozen-lockfile`, then `pnpm lint && pnpm typecheck && pnpm test && pnpm build`
- Browser tests: start the stack with the test stand-ins,
  `docker compose -f compose.yaml -f compose.e2e.yaml up --build --detach --wait`, then `cd web && pnpm e2e`
- After changing a route or a response model: `uv run python scripts/export_openapi.py`, then `cd web && pnpm gen:api`
- Ask the agent one question (calls the paid API): `uv run python -m standingproof.agent "<question>"`
- Re-record the checker's six cases (calls the paid API): `uv run python scripts/record_checker_cases.py`
- Grade the committed eval recordings against the baseline (free; CI runs it): `uv run python -m evals score`
- What an eval run would cost (free): `uv run python -m evals estimate`
- Run the eval (calls the paid API; it asks first): `uv run python -m evals run`, then
  `uv run python -m evals report` and, to make the result the baseline, `accept --note "<why>"`

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
- A change to a prompt, a tool description, an output schema, a model name or effort, or an
  eval case makes `python -m evals score` fail until the eval has been run again, because
  the committed recordings no longer answer for the code. Running it spends money: show
  the estimate and ask first. Do not get past the failure any other way.
- Never edit a file in `evals/recordings/` or `evals/baseline.json` by hand. `run` writes
  the first and `accept` the second, and a lower baseline needs a note that says why.
- Do not change an eval case, a label or a grader to make a result look better. A case
  that is wrong is fixed in its own commit, with the reason.
- `evals/` grades with the product's own `decide` and `judge_check`. Do not add a second
  way to accept a draft or to read a checker's reply there.
- The eval reads a GitHub and an AWS that exist only in `evals/world.py`. Nothing under
  `evals/` may call `build_backends` or build a real client for either.
- An eval result is a count out of a total, with the cases named. Never a bare percentage.
- A change to how a redraft is asked for (`REDRAFT_REQUEST`, `reviewer_section`) is judged
  by the eval's redraft cases (family G). They run through the graph, not the hand-written
  loop, with the world changed between the draft and the redraft; `expect` is about the
  redraft, and "cites a look" counts only a look the redraft made. A new case with no
  `redraft` must hash as it would have before the field existed.
- `standingproof/agent/model.py` is the only module that imports the `anthropic` SDK. Everything
  else depends on the `ModelClient` protocol in `agent/loop.py`.
- A model's final answer is accepted only by `check_draft` in `agent/steps.py`. New acceptance
  rules go there, in code, with a test. A sentence in the prompt is not a control.
- Every request to the model provider goes through the client's `FirstCallAlone`
  (`agent/warmup.py`): when the provider's cache is cold, one request writes it and the
  rest wait, for a bounded time. Do not call `messages.parse` around it.
- The checker's request is built only by `check_messages` in `agent/checker.py`, from the
  question, the draft's wording and the sources. Never pass it the drafter's messages, its
  note or a reviewer's instruction, and never give `CheckerClient` a tools parameter.
- Only `judge_check` in `agent/steps.py` gives a checker's reply a meaning. A check can move
  a question from `ready` to `needs_review` and never the other way; anything but a clean
  "supported" demotes. Say "the checker agrees", never "verified".
- A check is about one wording. `draft_checks` is append-only, and a finding is shown
  against an answer only when that answer is the wording that was checked.
- Text from outside (a question, a draft, a source) goes into a prompt through
  `prompts.wrap`, which stops it opening or closing one of our tags.
- `agent/steps.py` holds the loop's decisions as pure functions. The hand-written loop
  (`agent/loop.py`) and the graph (`workflow/nodes.py`) both call them, and
  `tests/workflow/test_graph.py` fails if the two disagree. Change a decision there, never in
  one caller.
- Writing answers back is plain code on rows a person approved: no model reads or writes in
  `standingproof/writeback/`. `plan.py`, `sheetxml.py` and `writer.py` take bytes and rows and
  return plain objects; only `load.py` and `service.py` touch the database.
- The writer changes only the bytes of the cells it writes. Never load a workbook with a
  spreadsheet library and save it to produce a file for a customer. `verify_unchanged` runs
  on every result before it is stored; a result that fails is not stored.
- A cell that cannot be written safely (occupied, a dropdown that does not offer the wording,
  too long) is listed with a reason. Never overwrite, shorten or reword to make it fit.
- A dropdown's value is written only by `plan.leading_choice`: the first word of approved
  wording, equal to one of the dropdown's own values. Never ask a model for it and never
  infer it from what a sentence means.
- `sent_files` records that a person said a file was sent. It is append-only. Say "recorded
  as sent", never "delivered".
- `filled_files` is append-only and keyed by its input (`load.file_key`). If the writer would
  produce different bytes for the same input, raise `WRITER_VERSION`. File and record are
  written in one transaction.
- Positions in a sheet's XML come from the parser (`sheetxml.scan`). A doctype or entity
  declaration is refused, cell references and styles are checked before use, and nothing
  from the file is put into XML we write without that check.
- Text read from an uploaded file is data. Store it and show it; never run it, format it into
  SQL, or treat it as instructions. A file's name and declared type are chosen by the sender and
  are never trusted.
- No model in `standingproof/drift/`. It looks at settings through `run_check`, so
  `checks/authorize.py` and the declared scopes decide every look, and it compares named
  facts in plain code.
- A webhook delivery is a hint, never a fact. Nothing from its body reaches evidence, a
  statement or a log line; `drift/github_events.py` takes a repository name and an action,
  each checked against a strict shape, and the watcher then reads the setting itself.
- The signature (`drift/signature.py`) is computed over the raw bytes of the request, before
  anything is parsed, and compared with `hmac.compare_digest`. A delivery that fails is
  answered 401 and nothing is stored.
- Reopening sets `review` to `pending` and `reopened_at`, raises the version, and touches
  nothing else: `status` is the agent's, `final_answer` is a person's. Only
  `drift/examine.py` reopens, one setting per transaction under its advisory lock.
- An observation and its `drift_queue` row are written in one transaction, and the row is
  removed (`drop_from_drift_queue`) in the transaction that examines its setting. Do not examine from inside
  `run_check`, and do not queue outside it.
- `drift_events` and `answer_reopenings` are append-only. `answer_evidence` is a pointer
  and is the only drift table updated in place. A webhook delivery is a wake-up, not a
  record: it is deleted some days after it was dealt with.
- The watcher looks for a webhook only at a setting some answer rests on, and marks every
  waiting delivery for a setting when one look has answered them: the ones that arrived
  before that look and had settled, never a later one. Keep both: they bound
  what a holder of the secret can make this deployment do.
- A change is examined against the answers that are waiting or decided at that moment.
  Wherever an answer comes to rest later (a draft saved, an earlier draft put back), call
  `watch.queue_newer` in the same transaction.
- Any code that offers "ready" questions for approval without opening them must leave out
  reopened ones (`reopened_at IS NULL`). `approve` is refused on a reopened question; `keep`
  restores what the first reopening recorded.
- Say "the setting changed" and show both values. Never "the answer is wrong".
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
- Code that needs vectors depends on the `Embedder` protocol in `library/embedding.py`, never on
  a model library. Tests use `tests/fakes.py::FakeEmbedder`; only tests marked `slow` load the
  real model, and no test calls a hosted embedding API.
- Vectors live in the `*_embeddings` tables, one row per item and model. A new model is an entry
  in `MODELS` plus a migration that creates its two partial indexes; a test fails if either is
  missing. Content tables never hold a vector.
- Content and its vector are written in one transaction: embed first, then insert both.
- A change to chunking, ranking or the embedding model is judged by `tests/eval`, and its numbers
  go in the pull request. Do not edit the golden set to make a change look better.
- Search excludes expired answers and other tenants' rows in SQL, not after loading.
- A regular expression that runs on uploaded text must not have two adjacent parts that can
  match the same characters. Prefer a fixed prefix and string methods.
- The Python version is in `.python-version` and the Dockerfile, and they must agree. CI checks.
- A dependency that runs native code may report usage to its vendor by default. Before adding
  one, run it under `strace -f -e trace=connect` and look at what it writes under the home
  directory. `ORT_DISABLE_TELEMETRY=1` is set in code and in the image for this reason; do not
  remove either.
- A check reads one setting and nothing else. Its API operations are declared in
  `checks/registry.py` and must be on `READ_ONLY_OPERATIONS`; the clients refuse anything
  else. Never add an operation that changes state, and never give a client a generic
  "call any endpoint" method.
- `checks/authorize.py` decides whether a check may run, before any client is touched. It is
  pure and default-deny. New rules go there with a test.
- A refusal, a timeout or a missing credential is a stored outcome with a reason, not an
  exception, and it asserts no fact. Only `observed` evidence is ever a source.
- Evidence keeps allow-listed fields of a reply (`checks/redact.py`), never the whole reply,
  and a statement is built from those fields only. Text from the remote system (a description,
  an error message) does not go into a statement, a log line or a tool result.
- Tokens and keys never appear in an error, a log line, evidence or a test fixture. Tests run
  with `tests/conftest.py::no_backends`, which cannot build a real client.
- Evidence is append-only. A new look is a new row.
- Check credentials are per deployment. A tenant must never be able to grant itself a scope:
  connecting one requires the operator's declaration (`Settings.declared_scopes`) and fails if
  another tenant holds it. Keep both rules until credentials are per tenant.
- A step of the graph can run more than once (after a crash, a retry, a second delivery). It
  reads the saved state, decides, and returns what changed. It must give the same result when
  repeated, and it never keeps anything in a variable outside the state.
- Graph state is saved to the database, so it holds plain JSON values only. Clients, tools and
  limits arrive through `QuestionContext`. Model content is saved through `plain_content` and
  sent back unchanged.
- Postgres is the record of what work exists and who holds it. A job on the Redis stream is
  only a wake-up and carries an id, never text. Code must be correct when a job arrives twice,
  late, or never: `leases.take_lease` decides who works, and the worker's sweep re-queues
  whatever is open and idle.
- Only the holder of a question's lease may save its result (`WHERE lease_owner = ...`).
- Whether a run is over is decided after a commit, never inside the transaction that finished
  a question: two last questions finishing together each see the other as open.
- The run's cost ceiling (`workflow/budget.py`) counts money already spent, including on
  attempts that did not finish, plus the most each question in progress could still cost. It is
  decided under a lock on the run. Do not start a question by any path that skips it.
- A provider failure that can pass (rate limit, outage) is retried later and ends as `failed`,
  never as "needs review". A person cannot fix a rate limit.
- Log an exception's type, not its message, in the workflow: a message can carry customer text.
- The app role holds no `UPDATE` on the append-only tables (`APPEND_ONLY` in
  `alembic/versions/0015_narrower_rights.py`); a new table of that kind joins that list in
  its own migration and in `tests/attacks/test_database_rules.py`.
- The app role holds `UPDATE` column by column (never on `tenant_id`) and `DELETE` on no
  table. A migration that adds a column to a tenant table adds
  `GRANT UPDATE (column) ON table TO standingproof_app`. Never grant `UPDATE` or `DELETE` on
  a whole table: either one also permits `LOCK TABLE`.
- Code that must remove a row calls a function owned by `standingproof_deleter`
  (`alembic/versions/0017_delete_by_function.py`): `SECURITY DEFINER`, a fixed
  `search_path`, static SQL with typed arguments, `EXECUTE` for the app role only, and a
  policy for the deleter with the tenant rule. Never make a running role a member of it.
- A migration statement that reads or changes rows of a tenant table, or creates an SQL
  function over one, must work for an owner that is not a superuser: lift `FORCE ROW LEVEL
  SECURITY` for that statement and restore it. `tests/test_migrations.py` runs every
  migration as such an owner.
- To empty a throwaway database in a test, use `EMPTY_THE_DATABASE` from `evals/world.py`.
  A plain `TRUNCATE tenants CASCADE` is refused, on purpose.
- "Latest", "first" and "newest first" are asked of a sequence, never of a timestamp:
  `seq` on `evidence`, `webhook_deliveries`, `review_decisions`, `draft_checks`,
  `filled_files`, `sent_files` and `answer_reopenings`. A new append-only table gets a
  `seq` identity column in its migration, and `INSERT` for the app role column by column
  without it (whole-table `INSERT` lets a statement name `seq`). A column added to one of
  these tables needs `GRANT INSERT (column)`; `tests/attacks/test_database_rules.py` fails
  until it has it. A page is cut by `seq`, never by a time, and the cursor a client holds is
  the last row's id: one sequence numbers every tenant's rows, so a `seq` shown to a client
  tells it how busy the others are. Never put a `seq` in a response.
- `run_questions.reopened_seq` is set and cleared with `reopened_at`. "The reopenings of
  this wait" are those with `seq >= reopened_seq`.
- A reopening is recorded once for a change in a wait, and again when the same change
  reopens the answer in a later wait (a redraft that rests on an older observation). Only
  `_reopen` in `drift/examine.py` decides that; the table has no unique rule for it. Code
  that lists a change's answers lists each answer once.
- `run_check` with `max_age` of zero always looks. It never asks the clock whether an
  earlier observation is recent enough.
- A new table with a `tenant_id` needs, in its migration: `ENABLE` and `FORCE ROW LEVEL
  SECURITY`, the `tenant_only` policy for `standingproof_app`, and a grant to that role. Copy
  `alembic/versions/0012_tenant_isolation.py`. `tests/attacks/test_database_rules.py` fails
  until it has them.
- Open a session with `database.tenant(tenant_id)`. `database.across_tenants` is for finding
  work only; a new module that uses it must be added, with its reason, to
  `tests/attacks/test_cross_tenant_modules.py`. Never give the system role a right on a table
  that holds customer content.
- No running service gets `SP_DATABASE_URL` (the owner). A migration that must read rows as
  a plain owner, or add a foreign key between existing tables, sets `NO FORCE ROW LEVEL SECURITY` for its own transaction and restores it.
- "Newest observation" means highest `evidence.seq`, never latest `observed_at`.
- An action a person or an operator decides is recorded with `audit.record(session, ...)`
  before its own `commit()`, never after and never in a second transaction. `details` holds
  strings, integers, booleans and nulls only; put a hash of customer text there, never the text.
  For anything short enough to guess (a name, an email address) put the id of its record, not
  a hash: a hash of a guessable value can be undone. A batch records all its entries in one `record()` call.
- Never use an advisory lock (`pg_advisory_*`) for something a tenant must not be able to
  block: any role can take one by name. Lock a row the policies govern.
- Never require one timestamp to be later than another for correctness. The clock is stepped
  back now and then; order is a sequence.
- Never give a running role `UPDATE` or `DELETE` on `audit_log`, and never write to `limits`
  as the app role: a tenant must not be able to lift its own ceiling.
- Every model call in the worker goes through `limits.guard` (`GuardedModel`,
  `GuardedChecker`). Do not hand the worker's raw model client to new code.
- A new `HandoffReason` needs its words in `web/src/lib/labels.ts`;
  `tests/test_reason_labels.py` fails until it has them.
- The saved-state tables belong to the workflow library and live in schema `workflow`. They are
  created by `python -m standingproof.workflow setup`, not by a migration.
- `LANGSMITH_TRACING=false` is set in the image. Never enable tracing to a hosted service:
  it would send customer text out.
- Only `workflow/review.py` approves anything. A worker, a graph step or a model result never
  sets `final_answer` or a review state other than `pending`.
- `review_decisions` is append-only. A decision and the question's review state are written
  in one transaction, and every change a reviewer could see raises the question's `version`.
- `status` is what the agent concluded; `review` is what a person concluded. Do not fold one
  into the other.
- A reviewer's instruction goes to the model only as `system_addendum`. Never put it in a
  message, and never put text from a questionnaire, a file or a tool result in the system
  message.
- At the `review` step the graph calls `interrupt()` first. On resume the step runs again from
  its first line, so nothing may come before that call.
- A live message says that a run changed and nothing else. Whoever receives it asks the API,
  where the tenant check is. Do not add data to it.
- A WebSocket route checks `Origin` against `Settings.allowed_origins` and the tenant before
  `accept()`. Use `anyio` task groups in it, not `asyncio.create_task`.
- Saved state of a question that is `pending` or `redrafting` is never pruned.
- `web/src/lib/api-types.ts` is generated from `web/openapi.json`, which is exported from the
  API. Never edit either by hand; a test and a CI step fail when one is stale.
- In `web/`, every colour comes from the tokens in `src/app/globals.css`. A component names a
  token (`bg-surface`, `text-muted`), never a colour.
- Text from the API is rendered as text. `dangerouslySetInnerHTML` fails the lint.
- A state is shown as a mark and a word as well as a colour. Every control is reachable and
  usable by keyboard, has a visible focus ring, and is at least 44 px tall on a phone. The
  browser test runs an accessibility check in both themes; a violation fails it.
- The browser calls only `/api/...` on the web app's own origin. The API's address
  (`API_URL`) is server-side only and must never appear in `web/.next/static`; CI searches.
- A new web dependency: check its publisher and repository, pin an exact version, and do not
  allow its install scripts. pnpm refuses a release less than a day old; do not override that.
- `NEXT_TELEMETRY_DISABLED=1` is set in the web scripts, the image and CI. Keep it.
- A browser test changes the product only through its API and its screen. It does not
  write to the database. What it needs from outside (a model's reply, GitHub's state) comes
  from `web/e2e/fakes/server.py`, which `compose.e2e.yaml` puts in place of the real thing.
  `compose.e2e.yaml` holds made-up test values and is never used for a deployment.

- The web app's forwarder (`web/src/app/api/[...path]/route.ts`) refuses a POST that the
  browser says came from another site (`Sec-Fetch-Site`, `Origin`) or that is not JSON. Any
  new method that changes something goes through the same check.
