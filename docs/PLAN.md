# Workbook Server — Specification & Work Plan

Read `CLAUDE.md` first. This document defines *what* to build and in which order.

---

## 1. Goal

Trainers write task catalogs ("workbooks") as YAML or JSON. The server picks up
new and changed files automatically, validates them and renders them as
interactive German web pages in the style of the reference workbook
(`docs/reference/`). Users log in; apprentices answer tasks and tick them off,
every change is autosaved to a per-user progress file; trainers review, comment
and sign off. New apprentices are created through the web UI via invite links.
Deployment: systemd service behind Caddy (automatic HTTPS). No Docker, no database.

---

## 2. Catalog format

- Full field reference with comments: `docs/workbook-template.yaml`.
  Treat it as the source of truth for the schema. Copy it to
  `workbooks/_template.yaml` (ignored by the server because of the `_` prefix).
- Accepted extensions: `.yaml`, `.yml`, `.json`. One workbook per file.
- Model it with Pydantic v2 (`extra="forbid"` so typos in keys become errors).
- Validation rules beyond types:
  - `workbook.id`, day ids, task ids, answer ids, header field ids: ID regex,
    unique within their scope; workbook ids unique across all files.
  - `module.code` unique within the workbook.
  - `task.level` must reference a key in `levels`.
  - `blocks` and any of `requirement`/`snippet`/`steps`/`guiding_questions`
    on the same task is an error.
  - `choice` needs `options`, `checklist` needs `items`, `image` needs `alt`.
  - `duration` format: `^(\d+h)?(\d+m)?$`, non-empty.
  - `workbook.version` must be a valid semantic version.
  - Image `src` must resolve inside `workbooks/assets/` (no path traversal).
  - `workbook.stylesheet`: only the keys listed in section 6a are allowed
    (`extra="forbid"`); every value must match
    `^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$`. Anything else is a validation error
    (prevents CSS injection).
- Computed values: task numbers (`<day index>.<task index within day>` unless
  `number` is set; optional days use their `nav_tag`), day load
  ("3 Aufgaben · ~2,5 h"), quick nav entries.
- Task content hash: SHA-256 of the normalized task content (excluding `trainer`)
  — used to detect tasks changed after a user answered them.

### Loader & hot reload
- On startup, load all files in `workbooks/`. Watch the directory with watchfiles.
- On change: re-parse that file. Valid → replace in registry. Invalid → keep the
  last valid version (if any) and record the error (file, key path, message,
  line number where possible).
- Deleted file → remove from registry (progress files are kept).
- `users.yaml` is watched the same way.
- Optional (phase 2): Server-Sent Events endpoint `/events` so open pages show a
  German banner "Dieses Heft wurde aktualisiert – neu laden" instead of
  reloading automatically (to avoid losing unsaved input).

---

## 3. Users, roles and authentication

### Files
`users.yaml` (human-editable, watched, written by the app via ruamel round-trip):
```yaml
users:
  mmustermann:
    name: Max Mustermann
    role: trainer          # trainer | apprentice
  mmueller:
    name: Max Müller
    role: apprentice
    workbooks: [network-security]   # optional; omitted = all workbooks
    active: true                    # default true
    supervisors:                    # optional; responsible trainer ("Fachbetreuer")
      network-security:             # per workbook of this apprentice
        default: mmustermann              # responsible for the whole workbook ...
        tasks:                      # ... unless a task names someone else
          lb02-terms: kschulz
```

`data/credentials.json` (app-managed only, mode 0600):
```json
{ "users": { "mmueller": {
    "password_hash": "$argon2id$...",
    "session_version": 1,
    "invite": { "token_hash": "sha256...", "expires_at": "2026-10-09T12:00:00Z" },
    "failed_attempts": 0,
    "locked_until": null } } }
```

`data/secret.key`: random 64 bytes, generated on first start if missing, mode 0600.

### Fachbetreuer (supervisors)
- "Fachbetreuer" is the German UI term for the `trainer` role (decided
  2026-10-06; the UI never says "Ausbilder"). Role values stay `trainer` /
  `apprentice`.
- Every workbook of an apprentice has at most one responsible Fachbetreuer
  (`supervisors.<workbook>.default`). Individual tasks may name a different one
  (`supervisors.<workbook>.tasks.<task-id>`). Resolution per task: task entry,
  else the workbook default, else nobody.
- One Fachbetreuer can be responsible for any number of apprentices.
- The assignment is informational (overview, "who should check this"). Every
  Fachbetreuer may see and review all tasks of all apprentices.
- Usernames in `supervisors` must be existing `trainer` users; unknown names or
  unknown workbook/task ids are shown as warnings in `/admin/users` and do not
  block login.
- Introduced with the `users.yaml` model in 0.3.0; shown in the review view in
  0.5.0. A per-user overview page (all tasks, status, Fachbetreuer) is planned
  after 1.0.0.

### Rules
- A user can log in only if present in `users.yaml`, `active: true`, and has a
  `password_hash`. Entries in `users.yaml` without credentials are shown in the
  admin UI as "Eingeladen, noch kein Passwort".
- Passwords: argon2id, minimum 12 characters, no other composition rules.
- Login failure always shows the same German message, regardless of whether the
  user exists. Lock for 15 minutes after 5 failed attempts per username; also
  an in-memory per-IP limit (20 attempts / 15 min). Read the client IP from
  `X-Forwarded-For` only because the app is bound to localhost behind Caddy.
- Sessions: signed cookie (itsdangerous) named `__Host-session`, flags
  `HttpOnly; Secure; SameSite=Strict; Path=/`, payload
  `{user, session_version, issued_at, last_seen}`. Idle timeout 8 h (refresh
  `last_seen` at most every 5 min). On every request: check user still exists,
  is active, and `session_version` matches — so deactivation and resets apply
  immediately.
- Logout: POST, clears cookie.
- Invites: 32 random bytes, URL-safe; only the SHA-256 is stored; single use;
  valid 72 h (configurable). Invite page lets the user set a password, then
  logs them in.
- Reset access: new invite, delete password hash, increment `session_version`.
- Deactivate: set `active: false` in `users.yaml`.
- Username suggestion when creating a user: first letter of first name + last
  name, transliterated (ä→ae, ö→oe, ü→ue, ß→ss), lowercase, ID regex; on
  collision append `-2`, `-3` ...
- Identity resolution lives in one dependency (`get_current_user`) so it can
  later be swapped for a trusted header from Caddy `forward_auth` (Authentik)
  via a config switch. Do not implement the header mode now, but keep the seam.

### Bootstrap CLI (`workbook`)
- `workbook user add <username> --name "..." --role trainer|apprentice [--workbook ID ...]`
  → adds to `users.yaml`, prints invite link.
- `workbook user reset <username>` → prints new invite link.
- `workbook user list`
- `workbook validate [PATH]` → validates catalogs, exit code 1 on errors.
- `workbook serve` → runs uvicorn with config.

---

## 4. Progress

`progress/<workbook-id>/<username>.json`:
```json
{
  "schema_version": 1,
  "workbook": "network-security",
  "workbook_version": "4.0.0",
  "user": "mmueller",
  "updated_at": "2026-10-06T10:12:00Z",
  "header": { "start-date": "2026-10-05", "period": "KW 41" },
  "tasks": {
    "sys01-boot": {
      "answers": { "order": "1. C – DNS ...", "checked-items": ["Aussteller"] },
      "done": true,
      "done_at": "2026-10-06T10:12:00Z",
      "task_hash": "sha256...",
      "updated_at": "2026-10-06T10:12:00Z",
      "review": { "status": "ok", "comment": "Sauber!",
                  "reviewed_by": "mmustermann", "reviewed_at": "2026-10-06T15:00:00Z" },
      "review_history": [
        { "status": "redo", "comment": "Bitte Kette ergänzen",
          "reviewed_by": "mmustermann", "reviewed_at": "2026-10-06T11:00:00Z" } ],
      "comments": [
        { "author": "mmueller", "role": "apprentice", "text": "Welche Kette ist gemeint?",
          "at": "2026-10-06T11:30:00Z" },
        { "author": "mmustermann", "role": "trainer", "text": "Die Zertifikatskette.",
          "at": "2026-10-06T12:00:00Z" } ]
    }
  },
  "signoff": { "comment": "", "date": null, "by": null }
}
```
- Answer value types: `text`/`short`/`date` → string, `checklist` → list of item
  strings, `choice` → string or list of strings (`multiple: true`).
- Unknown task/answer ids sent by the client → 422. Answers for tasks that no
  longer exist in the catalog are kept in the file but not rendered (trainer
  view shows them under "Verwaiste Antworten").
- Store `task_hash` whenever answers or `done` change. If the current catalog
  hash differs, show the German hint "Diese Aufgabe wurde geändert, nachdem du
  sie bearbeitet hast."
- Write access is split: apprentice endpoints may only touch `header`,
  `answers`, `done`; trainer endpoints may only touch `review` (incl.
  `review_history`) and `signoff`. Both roles may **append** to a task's
  `comments` (question/answer thread, "Rückfragen"): apprentices only to their
  own progress, Fachbetreuer to any apprentice they may review. `author` and
  `role` come from the session; messages are never edited or deleted. Text is
  trimmed, 1–5 000 characters.
- Review history: when a Fachbetreuer saves a review whose status or comment
  differs from the current one, the previous review is appended to
  `review_history` before it is replaced (clearing a review keeps it too).
  Autosaved comment edits by the same Fachbetreuer with an unchanged status
  within 10 minutes continue the current review instead of adding entries.
- A task whose last message is from the apprentice counts as "Rückfrage offen":
  Fachbetreuer see it in "Nur zu prüfen" (`/my-tasks?filter=open`) with a badge,
  and it counts as "zu prüfen" under "Meine Azubis". When the last message is
  from a Fachbetreuer, the apprentice sees an "Antwort" badge in `/my-tasks`.
- `review_history` and `comments` default to empty lists, so older progress
  files load unchanged.
- Size limits: 20 000 characters per answer, 1 MB per request.

### Autosave (frontend)
- Vanilla JS, debounce 800 ms per field, PATCH JSON with header `X-Workbook: 1`.
- Status indicator in the top bar: "Gespeichert" / "Speichert …" / "Offline –
  Änderungen werden erneut gesendet" (retry with backoff, keep pending changes
  in memory, warn on `beforeunload` if unsaved).
- Progress bar and module port LEDs update live (done = lit, review `redo` = red).

---

## 5. Routes

Pages (German UI, English URLs):
- `GET /login`, `POST /login`, `POST /logout`
- `GET /invite/{token}`, `POST /invite/{token}`
- `GET /` — workbook list with own progress (trainers: all workbooks, plus
  "Meine Azubis": apprentice workbooks they are Fachbetreuer for, with
  progress and open checks)
- `GET /my-tasks` — all tasks of the current user with status and Fachbetreuer
  (Fachbetreuer: the tasks they are responsible for, or with `?scope=all` all
  tasks of all active apprentices; filters `?filter=open|ok|redo` and
  `?sup=<username>|-` for the Fachbetreuer)
- `GET /workbooks/{workbook_id}` — workbook for the current user
- `GET /workbooks/{workbook_id}/theme.css` — generated color overrides (section 6a)
- `GET /workbooks/{workbook_id}/users/{username}` — trainer: view one user's
  workbook in review mode (review fields editable, answers read-only)
- `GET /workbooks/{workbook_id}/export` — static HTML snapshot of the current
  user's workbook with answers (trainer: `?user=` allowed), print-friendly
- `GET /admin/overview` — matrix users × tasks per workbook (port LEDs, done
  dates, review status), links to review view
- `GET /admin/users`, `POST /admin/users` (create → shows invite link with copy
  button), `POST /admin/users/{username}/reset`,
  `POST /admin/users/{username}/deactivate`, `POST /admin/users/{username}/activate`,
  `GET|POST /admin/users/{username}` (apprentice: name, workbooks, Fachbetreuer
  per workbook and per task)
- `GET /admin/catalogs` — loaded catalogs, versions, validation errors
- `GET|POST /admin/editor`, `GET /admin/editor/{workbook_id}` — form editor
  (create blank/copy, edit); JSON API `GET|PUT /api/editor/{workbook_id}`,
  `POST /api/editor/preview`, `GET /api/editor/strings`. Saves validate
  first, back up the old file, keep YAML comments, refuse when the file
  changed meanwhile and require confirmation before removing tasks or answer
  fields that already have saved answers.
- `GET /assets/{path}` — catalog images from `workbooks/assets/` (login required)

JSON API (all require session + `X-Workbook: 1` + same `Origin`):
- `PATCH /api/progress/{workbook_id}/header`
- `PATCH /api/progress/{workbook_id}/tasks/{task_id}` — `{answers?, done?}`
- `PATCH /api/progress/{workbook_id}/users/{username}/tasks/{task_id}/review` — trainer
- `PATCH /api/progress/{workbook_id}/users/{username}/signoff` — trainer
- `POST /api/progress/{workbook_id}/tasks/{task_id}/comments` — apprentice,
  `{text}`; appends to the own thread, returns the stored message
- `POST /api/progress/{workbook_id}/users/{username}/tasks/{task_id}/comments`
  — trainer, `{text}`; appends to that apprentice's thread (same access checks
  as the review endpoint)
- `GET /events` — SSE catalog change notifications (phase 2, optional)

All `/admin/*` and trainer endpoints → 403 for apprentices.
Apprentices requesting a workbook not in their `workbooks` list → 404.

---

## 6. Rendering & UI

Reproduce the structure of the reference workbook:
sticky top bar (brand, progress bar, save status, user name, logout), quick nav
of days, document header (title, subtitle, header fields, intro), day banners
(title, subtitle, computed load, optional badge), modules (code, title,
objective, port LEDs), tasks (number, title, level badge, duration, content,
collapsible hints, answer fields, done checkbox, collapsible bonus, trainer area
only for trainers), sign-off section, footer.

- Trainer area for trainers: expectations, notes, review status select
  (— / OK / Nacharbeiten) and comment.
- Apprentices see review status and trainer comment read-only once set
  (expectations and notes never).
- Level badge colors from the catalog (`blue | ochre | green | grey | red`),
  muted tones derived from the design tokens.
- Validation errors on `/admin/catalogs` in German with the English key path.
- Print stylesheet as described in `CLAUDE.md`.

---

## 6a. Per-workbook stylesheet

Every workbook may define `workbook.stylesheet` to deviate from the default
color scheme. If the parameter is missing, the defaults are used. If only some
keys are set, only those are overridden; every missing key keeps its default.

```yaml
workbook:
  stylesheet:
    paper: "#F7F5F0"
    accent: "#0F4C3A"
    level_palette:
      blue: "#2B5C8A"
```

Allowed keys (YAML key → CSS custom property):

| Key | Property | Used for |
|---|---|---|
| `paper` | `--paper` | page background |
| `card` | `--card` | cards, task boxes |
| `ink` | `--ink` | body text |
| `muted` | `--muted` | secondary text, micro labels |
| `line` | `--line` | rules, borders |
| `accent` | `--accent` | top bar, headings, buttons, links, progress |
| `accent_hover` | `--accent-hover` | hover/active states |
| `accent_soft` | `--accent-soft` | tinted accent backgrounds |
| `ok` | `--ok` | done / review OK, lit port LEDs |
| `redo` | `--redo` | review "Nacharbeiten", red port LEDs |
| `hint_bg` | `--hint-bg` | hints box |
| `trainer_bg` | `--trainer-bg` | trainer area |
| `bonus_bg` | `--bonus-bg` | bonus box |
| `level_palette.blue` … `.red` | `--level-blue` … `--level-red` | level badges |

Rules:
- Defaults stay in `app/static/css/tokens.css`. `accent_hover` and
  `accent_soft` default to `color-mix()` expressions of `--accent`, so setting
  only `accent` yields matching hover and tint colors automatically. If they are
  set explicitly, the explicit value wins.
- The server generates `GET /workbooks/{workbook_id}/theme.css` from the
  validated model: a single `:root { … }` block containing only the set keys.
  If no stylesheet is defined, the response is an empty stylesheet.
  Response headers: `Content-Type: text/css`, `Cache-Control: no-cache` plus an
  `ETag` based on the stylesheet hash. The page links it as
  `theme.css?v=<hash>` after `tokens.css`.
- All pages that belong to a workbook (workbook view, trainer review view,
  export) use its theme. Pages without a workbook context (login, start page,
  admin) always use the defaults.
- Static outputs (`workbook render` CLI in 0.1.0 and the export in 1.0.0) inline
  tokens + theme into the exported file, because they are opened without the server.
- `/admin/catalogs` shows a **warning** (not an error) when a stylesheet results
  in a WCAG contrast ratio below 4.5:1 for `ink` on `card`, `ink` on `paper`,
  white text on `accent`, or `accent` on `card`. Compute it on the effective
  colors (overrides merged with defaults; skip `color-mix` derived values).
- The theme changes colors only. Fonts, spacing, radius and layout are not
  configurable per workbook.

---

## 7. Configuration (`config.yaml`)

```yaml
base_url: https://arbeitsheft.example.de   # used for invite links and Origin check
listen_host: 127.0.0.1
listen_port: 8000
paths:
  workbooks: workbooks
  progress: progress
  users: users.yaml
  data: data
  locales: locales
session_idle_minutes: 480
login_max_attempts: 5
login_lockout_minutes: 15
invite_valid_hours: 72
auth_mode: local        # reserved: later "header" for forward_auth
secure_cookies: true    # __Host- cookies with Secure flag; false only for plain-HTTP testing
```
Provide `config.example.yaml`; `config.yaml` is git-ignored, as are
`progress/`, `data/` and `users.yaml`.

---

## 8. Deployment (`deploy/`)

- `deploy/Caddyfile`:
  ```
  arbeitsheft.example.de {
  	reverse_proxy 127.0.0.1:8000
  	header {
  		Strict-Transport-Security "max-age=31536000"
  		X-Content-Type-Options nosniff
  		X-Frame-Options DENY
  		Referrer-Policy no-referrer
  		Content-Security-Policy "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
  		-Server
  	}
  	log {
  		output file /var/log/caddy/arbeitsheft.log
  		format json
  	}
  }
  ```
  (CSP requires no inline scripts/styles in templates — use static files.)
- `deploy/workbook.service`: dedicated system user `workbook`, `WorkingDirectory`,
  venv `ExecStart`, `Restart=on-failure`, hardening (`NoNewPrivileges`,
  `ProtectSystem=strict`, `ReadWritePaths=` for progress/data/users.yaml,
  `PrivateTmp`, `ProtectHome`).
- `deploy/README.md` (English): install steps, DNS + port 80/443 forwarding,
  first trainer via `workbook user add`, backup of `progress/`, `data/`,
  `users.yaml` (e.g. restic or nightly git commit), optional fail2ban filter
  for the app's failed-login log line.
- App logs failed logins as one structured line:
  `auth.login_failed user=<name> ip=<ip>`.

---

## 9. Phases

Each phase ends with: ruff clean, tests green, version bump, CHANGELOG entry,
commit, short summary, STOP.

### 0.1.0 — Schema & static renderer
- Project skeleton, `pyproject.toml`, ruff config, `locales/de.yaml`, i18n helper.
- Pydantic catalog models + validation rules (section 2), including the
  `stylesheet` model (section 6a).
- `workbook validate` CLI.
- Renderer + templates + CSS (tokens only, no hex outside `tokens.css`) +
  self-hosted fonts/icons; CLI `workbook render <file> -o out.html` producing a
  static preview with tokens and theme inlined.
- Convert the reference workbook from `docs/reference/` into
  `workbooks/network-security.yaml` (all days, modules, tasks, hints, bonus,
  trainer expectations — content must be complete and unchanged).
- **Accept:** template and network-security validate; rendered output matches
  the reference structure and content; tests cover every validation rule,
  duration/load calculation and theme generation (no stylesheet, partial,
  full, invalid values rejected); rendering the template with a partial
  stylesheet visibly changes only the overridden colors.

### 0.2.0 — Web server & hot reload
- FastAPI app, config loading, registry, watcher, `/admin/catalogs` (temporarily
  without auth, bound to localhost) incl. contrast warnings, workbook pages
  rendered from registry, `theme.css` endpoint with ETag.
- Optional SSE update banner.
- **Accept:** adding/changing/breaking/deleting a file is reflected without
  restart; broken file keeps last valid version and shows the error.

### 0.3.0 — Authentication & user management
- `users.yaml` model + watcher, credentials store, secret key, login/logout,
  sessions, rate limiting, invites, CSRF/Origin checks, bootstrap CLI,
  `/admin/users` (create, reset, deactivate, activate, copy invite link).
- All pages require login.
- **Accept:** tests for login success/failure, lockout, session invalidation on
  reset/deactivation, invite expiry and single use, role checks, path-traversal
  rejection for usernames, ruamel keeps comments in `users.yaml`.

### 0.4.0 — Progress & autosave
- Progress store (atomic writes, locking, permissions), apprentice API,
  autosave JS with status indicator and retry, live progress bar/LEDs,
  changed-task hint, orphaned answers handling.
- **Accept:** two users never see each other's data; concurrent PATCHes don't
  corrupt files; reload shows saved state; tests for split write permissions
  and size limits.

### 0.5.0 — Trainer features
- Review view per user, review/sign-off API, `/admin/overview` matrix,
  apprentice read-only display of review status/comment.
- **Accept:** trainer content never appears in apprentice HTML (test asserts on
  rendered output); apprentices get 403 on all trainer endpoints.

### 0.6.0 — Deployment
- `deploy/` files, `config.example.yaml`, `deploy/README.md`, CSP-compatible
  templates verified (no inline JS/CSS).
- **Accept:** fresh install following the README works on Ubuntu; app runs as
  unprivileged systemd service behind Caddy.

### 1.0.0 — Export & polish
- Export endpoint (static HTML snapshot with answers, print CSS, workbook
  theme inlined), JSON Schema
  export of the catalog model (`workbook schema -o workbook.schema.json`) with
  instructions for VS Code YAML autocompletion, final docs (`docs/AUTHORING.md`
  in English: how to write a workbook), accessibility pass (labels, focus
  states, contrast).
- **Accept:** exported HTML opens offline and prints cleanly.
