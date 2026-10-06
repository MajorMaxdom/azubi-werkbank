# CLAUDE.md — Workbook Server

## What this project is
A small self-hosted web server that turns human-readable YAML/JSON task catalogs
("workbooks") for IT apprentices into interactive HTML pages. Each user logs in,
answers tasks, ticks them off, and their progress is autosaved to a per-user file.
Trainers review answers and sign off.

The full specification and the phased work plan live in **`docs/PLAN.md`**.
Read it completely before starting. The field reference for catalogs is
**`docs/workbook-template.yaml`**. The visual and structural reference is the
hand-written HTML workbook in **`docs/reference/`** (read-only, never modify).

## How to work
- Work strictly phase by phase as defined in `docs/PLAN.md`.
- At the end of each phase: run `ruff check`, `ruff format --check` and `pytest`,
  make sure all acceptance criteria are met, commit, then STOP and give a short
  summary (what was built, what was tested, open questions). Wait for approval
  before starting the next phase.
- If the spec is ambiguous or you want to deviate from it, ask first.
- Do not add features that are not in the plan.
- Commit messages: English, Conventional Commits (`feat:`, `fix:`, `docs:` ...).
- Versioning: Semantic Versioning. Each phase bumps the version in
  `pyproject.toml` to the version given in the plan and adds a `CHANGELOG.md` entry.

## Language rule (important)
- **Everything visible in the browser is German**: labels, buttons, messages,
  validation errors shown to users, page titles.
- **Everything else is English**: code, identifiers, comments, docstrings, file and
  directory names, YAML/JSON keys, enum values (`trainer`, `apprentice`, `ok`,
  `redo`), log messages, CLI commands and help texts, URLs, commit messages, docs
  in `docs/`.
- Templates must not contain hard-coded German text. All UI strings come from
  `locales/de.yaml` via a translation helper (`t("task.mark_done")`).
- Catalog *content* (task texts written by authors) is German and rendered as-is.

## Tech stack (do not change without asking)
- Python 3.12, FastAPI, Uvicorn, Jinja2 (autoescape ON), Pydantic v2
- ruamel.yaml (round-trip, preserves comments when the app writes `users.yaml`)
- watchfiles (hot reload of catalogs and `users.yaml`)
- argon2-cffi (password hashing), itsdangerous (signed session cookies)
- markdown-it-py with raw HTML **disabled**
- typer (CLI), python-multipart (forms)
- Dev: pytest, httpx, ruff
- Packaging: `pyproject.toml`, plain venv. **No Docker. No database.**
- Frontend: server-rendered HTML + vanilla JavaScript. **No frontend framework,
  no Tailwind, no CSS framework, no build step, no CDN.** Fonts and icons are
  self-hosted under `app/static/`.

## Repository layout
```
app/
  main.py            FastAPI app factory
  config.py          loads config.yaml
  models/            Pydantic models (catalog, users, progress, credentials)
  loader.py          parse + validate catalogs, registry, file watcher
  renderer.py        markdown, duration parsing, day load calculation
  auth.py            login, sessions, invites, rate limiting, user resolution
  progress.py        per-user progress files, atomic writes, locking
  i18n.py            translation helper for locales/de.yaml
  api/               route modules (pages, progress, admin, auth)
  templates/         Jinja2 templates
  static/            css/, js/, fonts/, icons/
cli.py               `workbook` CLI entry point
workbooks/           catalogs (*.yaml, *.json; files starting with "_" ignored)
workbooks/assets/    images referenced by catalogs
progress/            progress/<workbook-id>/<username>.json
users.yaml           user list (human-editable)
data/                credentials.json, secret.key (app-managed, mode 0600)
locales/de.yaml      all German UI strings
deploy/              Caddyfile, systemd unit, install notes
docs/                PLAN.md, workbook-template.yaml, reference/
tests/
```

## Security rules (non-negotiable)
- App listens on `127.0.0.1` only; Caddy terminates TLS in front of it.
- The current user is resolved in exactly one place (auth middleware/dependency).
  Never take the acting username from the URL, query or request body.
- Apprentices may only read/write their own progress. Trainers may read all
  progress but only write `review` and `signoff` data. Both roles may append
  messages to a task's question thread (`comments`); author and role always
  come from the session.
- `trainer` content of catalogs (expectations, notes) is never sent to apprentices,
  not even hidden in HTML.
- Usernames and IDs are validated with `^[a-z0-9][a-z0-9-]{0,62}$` before being
  used in any file path.
- User answers are always escaped. Markdown is rendered only for catalog content.
- All state-changing requests: POST/PATCH only, require same-origin `Origin`
  header and the custom header `X-Workbook: 1` (JSON API) or a CSRF token (forms).
- Files are written atomically (temp file + `os.replace`) under a per-file lock.
- Files in `data/` and `progress/` are created with mode 0600 / dirs 0700.
- Never log passwords, tokens or session cookies.

## Design rules
Light theme only. Must look hand-crafted, not AI-generated. Dark blue accent by
default; colors can be overridden per workbook.
- CSS custom properties only. The defaults live in `app/static/css/tokens.css`:
  `--paper:#F4F6F8 --card:#FFFFFF --ink:#1A2433 --muted:#5B6878 --line:#D6DCE3
   --accent:#1B365D
   --accent-hover:color-mix(in srgb, var(--accent) 75%, black)
   --accent-soft:color-mix(in srgb, var(--accent) 10%, white)
   --ok:#1F7A4D --redo:#B5541C --hint-bg:#EEF3F9 --trainer-bg:#FAF5EA
   --bonus-bg:#F3F4F7
   --level-blue:#2F5D8A --level-ochre:#9A6A12 --level-green:#2E7D52
   --level-grey:#5B6878 --level-red:#A8432A`
  Level badge backgrounds/borders are derived with `color-mix()` from the level
  color, never hard-coded.
- **Every component uses tokens only — no hex values anywhere else in the CSS.**
  This is what makes per-workbook color schemes possible.
- Per-workbook color overrides (`workbook.stylesheet` in the catalog, see
  `docs/PLAN.md` section 6a) are delivered as a generated stylesheet
  `/workbooks/{id}/theme.css` loaded *after* `tokens.css`. It contains only the
  keys that are set; everything missing falls back to the defaults through the
  cascade. Never inject catalog colors as inline `style` attributes or inline
  `<style>` blocks (CSP forbids it). Only validated hex colors may reach CSS.
- Fonts: Inter (UI) and JetBrains Mono (labels, IDs, codes, snippets), self-hosted woff2.
- Icons: Lucide, inline SVG, sparingly.
- Border radius max 4px. No shadows, no gradients, no glassmorphism, no emoji in
  the UI chrome, no pill-shaped everything.
- Thin 1px rules, worksheet feel, generous whitespace, monospace uppercase micro
  labels (as in the reference). Keep the "port LED" progress per module.
- Responsive down to 360px; clean print stylesheet (no top bar, no buttons,
  hints/bonus expanded, trainer area hidden unless trainer export).
