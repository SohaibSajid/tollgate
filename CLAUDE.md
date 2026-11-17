# Tollgate (FYP) — read this first

Prepaid AI gateway: client app (`tg_` key) → Tollgate → AI provider. Reserve worst-case credits, call provider once, settle exactly once. Student: Sohaib Sajid. Current client app: Flutter "Saathi" (Avela) through its Docker backend.

Do not re-read the whole repo. Use these files instead:

| need | file |
|---|---|
| code map, modules, invariants, env settings | `docs/dev/PROJECT_MAP.md` |
| current state, decisions, supervisor feedback, roadmap | `docs/dev/STATUS_AND_ROADMAP.md` |
| BYOK and self-service accounts (built) | `docs/dev/DESIGN_BYOK_ACCOUNTS.md`, `docs/CLIENT_PORTAL.md` |
| client API (source of truth) | `docs/api/openapi.json`, `docs/API_GUIDE.md` |
| connecting a Dockerised backend | `docs/integrations/SAATHI.md` |
| integration modes, fail-open wrapper, key ownership | `docs/integrations/INTEGRATION_MODES.md` |
| paste-ready prompt for a client developer's assistant | `docs/integrations/CLIENT_PROMPT.md` |
| design, offline/fail-open | `docs/ARCHITECTURE.md`, `docs/OFFLINE.md`, `docs/WARDENS.md` |

## User rules

- Targeted edits only, clean code, no comments, simple variable names.
- Do not change billing, settlement, authentication or the API contract without asking first.
- Code changes: add or update tests, run `python -m unittest discover -s tests` from `gateway/` (151 pass, 1 skipped).
- Client-facing API changes: edit `gateway/app/api_contract.py`, run `python manage.py export-openapi`, run tests.
- Never ask for, print or log keys (owner key, `tg_` keys, provider keys). Owner key: user runs `manage.py admin-key` themselves.
- Windows PowerShell commands. Short, practical answers. Finish by listing changed files and next steps.
- Commit or push only when asked. Commit messages end with the Co-Authored-By line from the session.

## Dev setup (Windows, SQLite, live Gemini free tier)

From `gateway/` (venv at `gateway\.venv`):

```powershell
.\.venv\Scripts\python.exe manage.py serve --host 0.0.0.0 --port 8100
.\.venv\Scripts\python.exe manage.py worker
.\.venv\Scripts\python.exe manage.py doctor
```

- `gateway/.env`: `TOLLGATE_PUBLIC_ORIGIN=http://host.docker.internal:8100`, DB `data/gemini-test.db`, model `gemini-3.5-flash-lite`, session 8 h. Restart `serve` after any `.env` change.
- Windows hosts file maps `127.0.0.1 host.docker.internal` so the browser and Docker use the same origin. Dashboard sign-in needs the Origin header to equal the public origin exactly.
- Saathi backend (Docker, port 8000) calls `http://host.docker.internal:8100/v1/chat/completions`; phone reaches it via `adb reverse tcp:8000 tcp:8000`.
- Prices in `gateway/models.json` were reviewed 2026-10-22; live requests stop after 2026-11-21 (30-day rule). Re-review before then.
