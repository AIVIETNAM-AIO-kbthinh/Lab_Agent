# Lab Agent Platform

An AI agent for research labs: a Q&A chatbot (knowledge base from Google Drive + the lab website), scheduled jobs on
Google Workspace, and paperwork automation (form engine: membership applications, equipment loan forms, meeting
minutes; equipment returns/extensions; application approval, member offboarding, role changes).
Design: `isilab_agent_design.md`. Implementation plan: `code_plan.md`.

Each lab runs its own instance (separate database, Qdrant collection and Google account) on the same codebase.
Adding a lab means adding a `config.yaml` + `.env`, with no code changes.

## Setup (dev)

```bash
uv sync                                    # Python 3.12, includes the dev group
cp .env.example .env                       # fill in LLM_API_KEY, APP_SECRET_KEY, QDRANT_API_KEY...
cp config.example.yaml config.yaml         # edit lab, drive.root_folder_id, auth.admins
mkdir -p secrets && mv credentials.json token.json secrets/   # no token yet: uv run python scripts/get_token.py
docker compose -f deploy/compose.dev.yaml --env-file .env up -d   # Postgres 16 + Qdrant
uv run labagent db upgrade
uv run labagent forms publish              # insert placeholders into template copies → templates/_system/ → activate
uv run labagent preflight --email <leader-email> --email <member-email>
```

Form previews are PDF when LibreOffice is installed (`soffice` is detected automatically, or set
`render.soffice_path`); otherwise previews are DOCX (the "store exactly what was previewed" rule still holds).

`preflight` checks the config (printing each final value with its source: env/yaml/default), secrets, migrations,
Qdrant, the Google token and scopes, every Drive path, the column headers of every sheet, Members (duplicate names,
exactly one leader), and makes test calls to the LLM, embedding and rerank APIs. Any ✗ gives exit code 1.

## Running

| Command | What it does |
|---|---|
| `uv run labagent serve` | API + web chat at http://127.0.0.1:8000 (`/login`, `/admin/kb/web`) |
| `uv run labagent worker` | Single worker (advisory lock): outbox + job schedule + crawl + indexing |
| `uv run labagent job list` / `job run <name> [--today YYYY-MM-DD] [--dry-run]` | Run a job manually |
| `uv run labagent crawl [--full] [--dry-run] [--force]` / `crawl --apply-run <id>` | Crawl the lab website |
| `uv run labagent kb search "question" --role guest` | Query the KB (checks permission filtering) |
| `uv run labagent chat "question" --role guest` / `--as "<full name>"` | Chat with the agent from the CLI |
| `uv run labagent account create --email ...` (`reset`, `disable`, `enable`, `unlock`, `delete`, `list`) | Admin-issued accounts (only for emails in Members or `auth.admins`) |
| `uv run labagent forms check` / `forms publish [--form id] [--dry-run]` / `forms list` | Form schemas (`schemas/`), publish templates to Drive, list active forms |
| `uv run labagent forms render --form id --template form.docx` | Insert placeholders + render sample data from a local file (no Drive needed) |
| `uv run labagent mail test --to <email>` | Send one `[TEST]` email |
| `uv run labagent smoke llm` | Check LLM gateway features, writes `docs/gateway-compat.md` |
| `uv run labagent eval run` | 30-question agent eval (costs LLM credits), report in `var/eval/` |
| `uv run labagent eval rag [--retrieval-only]` | KB RAG eval: 72 questions (`tests/eval/rag_questions.yaml`), measures Hit@k/MRR/context recall + end-to-end answers, report in `var/eval/rag-*.md` |
| `uv run labagent job run kb_reconcile --param force=1` | Re-read every KB file on Drive (after changing chunking or permission labels) |
| `uv run labagent reindex --all` | Rebuild the whole index into a new collection (embedding model change), then switch the alias |

## Bypassing auth during development (`test.auth_bypass`)

While working on features (RAG, tools, jobs...), set `test.auth_bypass: true` in `config.yaml` (requires
`test_mode: true` and `APP_ENV` other than `production`; production refuses to start with it enabled). Then:

- No login or account is needed; a role picker (Guest / Member / Secretary / Leader / Admin) appears at the top of the
  chat UI. The member roles map to the first person with that role in the Members sheet (if it can be read).
- Chat rate limits are lifted. Login, member/guest resolution, CSRF and session code stay unchanged and keep their own tests.
- Set it back to `false` to test the real auth flow.

## Duty roster and weekly report (manual runs)

Every recipient email is looked up by full name in the `data/Members` sheet at run time (fresh read, no cache).
Weekly order (cron schedule comes later): create tab → announce → remind → assign `T` + report to the leader and secretary.

Members only mark `V` (or `v`) on the sessions they **can** cover. Official duty sessions `T` are assigned by the agent
(turning `V` into `T`): every session gets at least one `T`, every member gets at least one `T`, and members who sign
up for more sessions get more `T`s. Quota = sessions signed up / `sheets.duty.registrations_per_t` (default 3,
rounded, minimum 1: 3→1, 5–7→2, 8–10→3). Sessions beyond the total quota are shared out in proportion to T/quota, so
the extra falls on members who signed up for more. Existing `T` cells (assigned by the leader) are kept and counted;
re-running only assigns what is still missing. Sessions with no sign-ups and members with no sign-ups are flagged
"CẦN XỬ LÝ" (needs attention) in the report.

| Command | What it does |
|---|---|
| `labagent job run duty_create` | Create next week's tab from `templates/LichTrucTuan` and fill in Members' names (last week's `T`s are not copied unless `sheets.duty.copy_fixed_from_prev` is on) |
| `labagent job run duty_announce` | Email **every** member asking them to mark `V` on the sessions they can cover (tab link, deadline) |
| `labagent job run duty_remind` | Remind members below `min_sessions_per_week` (including members with no row in the tab) |
| `labagent job run duty_assign` | Assign `T` only (no email); `--dry-run` shows the plan without writing to the sheet |
| `labagent job run weekly_report` | Assign `T` for next week's tab, then send a report to `notifications.weekly_report_to` (default: leader + secretary): assigned roster, who is short on sessions, who is borrowing what, items due by the end of next week, overdue items, loans/returns this week, new applications, data to fix. `--dry-run` only computes the `T` plan, no writes |

- `--dry-run`: send nothing, print the first few emails verbatim. `--today 2026-10-30`: simulate the run date (next week is computed from it).
- `--param only="Full Name"`: send only to that person. `--param resend=1`: send again even if already sent (each email is sent once per week).
- `duty_remind --param round=2`: second reminder round (separate lock, does not touch round 1).
- A member with no email in Members → nothing is sent, `AuditLog` records FAILED. A name in the roster tab that does not match Members → FAILED, shown under "Data to fix" in the report.

## Form engine and write operations (P3)

- **Two ways to submit a form:** download the template (a backend-generated button with a signed, expiring link) and
  send back the filled file (📎 button in chat), or give the details in chat and let the assistant fill it in. Each
  form's schema lives in `schemas/*.yaml` (anchors match the template text on Drive verbatim).
- **Nothing is written until confirmed:** the draft lives in the database; the assistant sends a preview with
  **Confirm / Cancel** buttons. The button is bound to the hash of the exact version shown: editing after the preview
  invalidates the old button. Double-clicking creates only one record.
- **Writes go through the outbox** (requires `labagent worker`): upload the file (folder from the schema's `output`)
  → `FormSubmissions` row → hook (`application` / `equipment_borrow` / `meeting_minutes`) → `AuditLog`. Before
  writing, the worker re-reads live data (e.g. whether the equipment is still available): if two people borrow the
  last unit at once, exactly one succeeds.
- **Equipment returns/extensions, application approval, offboarding, leader/secretary changes:** the tool only creates
  a request and a summary; the write happens when the user clicks Confirm (`/actions/{id}/confirm`).
- The monthly `retention` job moves rejected applications older than `applications.rejected_retention_months` to `_archive/`.

## On-demand exports

Members can ask things like "export the K67 member list", "file of equipment currently on loan", "September loan
history as CSV" → the `export_data` tool filters the large sheets (Members, Thietbi, LichSuMuon) and shows a download
button for an Excel (default) or CSV file.

- Data: `members` (filter by status/role/school/class), `devices` (name, condition, available/on loan/not loanable),
  `borrows` (on loan, overdue, returned, all; by borrower, device, loan date). Columns and file name can be chosen.
- Same permissions as the lookup tools: phone numbers, student IDs and former members are visible only to the
  secretary/leader/admin; regular members can only export their own returned-loan history; Address is never
  exported; guests do not get this tool.
- **Submitted forms** (`export_form_files` tool): "export Nguyen Van A's equipment loan forms from last week",
  "September meeting minutes", "form P-20261016-01". Filter by form type, submitter, submission date, form code; one
  form → one file, several → a `.zip` (at most `exports.max_files`). Exports the EXACT file stored on Drive (PDF stays
  PDF, DOCX stays DOCX; Google Docs export as DOCX), never rebuilt from data; forms whose file is gone from Drive are
  skipped and reported. Permissions: your own forms — anyone; meeting minutes — all members; other people's forms —
  secretary/leader/admin; membership applications — leader/admin only. Only Drive files whose appProperties carry the
  right form code (uploaded by the system) are downloaded.
- Files are stored in the database, downloadable only by the session that requested them (`/exports/{id}`), and
  deleted after `exports.ttl_minutes`. File contents are never sent to the LLM. Cells starting with `=`, `+`, `-`, `@`
  are never turned into formulas.

## KB document permissions

- By folder: `kb/public/**` → readable by guests, `kb/internal/**` → members only.
- By section: a heading labelled `[Nội bộ]` / `[Internal]` makes that whole section (including subsections)
  members-only, even when the file is in `kb/public`. Labels only restrict access, they never open internal documents.
  A chunk never mixes content with different labels.

## Testing

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy app
uv run pytest            # Postgres via testcontainers (requires Docker); `live` tests are skipped by default
```

## Project layout

```
app/
  config.py            Settings (.env) + AppConfig (config.yaml), validated at startup
  db/                  models + Alembic (migrations/)
  google/              oauth, retry, drive, sheets, docs, gmail, paths (Protocols ⇒ swapped for fakes in tests)
  sheets/              SheetTable: read/write by column header
  domain/              names, dates, members, equipment, duty, submissions, audit
  auth/                roles, passwords (argon2id), sessions, csrf, ratelimit, accounts, deps
  llm/                 client (profile → adapter, cross-vendor fallback), anthropic/openai adapters, usage, embeddings, rerank, smoke
  rag/                 extract_text, chunker, sparse (Vietnamese BM25), store (Qdrant), indexer, retriever, crawler, drive_source
  tools/               registry (permissions, re-validates arguments), builtin (search_kb, query_*), forms, actions
  forms/               schema, placeholder, validators, drafts, render (docxtpl + LibreOffice), extract, engine,
                       submit (outbox), publish, registry, links, schema_gen/ (schema generation from templates)
  hooks/               application, equipment_borrow, meeting_minutes
  workflows/           actions (requests awaiting confirmation), equipment (return/extend), members (approve/offboard/change role)
  agents/              loop (tool use + SSE), router, conversation (append-only), prompts/
  outbox/              multi-step write queue, idempotent per (op_id, step)
  jobs/                duty, equipment, weekly_report, kb_sync, web_crawl, health, cleanup, retention, scheduler
  api/ web/            FastAPI + plain HTML/JS
schemas/               hand-written schemas for the 3 initial forms (P4 generates the same format)
tests/                 unit, integration (real Postgres), fakes, fixtures (isilab.click HTML, 3 blank forms), eval/
deploy/compose.dev.yaml
```
