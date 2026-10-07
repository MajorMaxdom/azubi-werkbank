# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

## [1.12.0] - 2026-10-07

### Added
- Own certificate instead of Caddy: with `tls_cert` and `tls_key` in the
  config the app serves HTTPS itself (wildcard or host certificate, PEM).
- Installer: asks how the site is reached – HTTPS via Caddy, HTTPS with an
  own certificate, or only locally (`--https caddy|cert|none`, `--cert`,
  `--key`). Checks that key and certificate match, that the certificate is
  valid and covers the domain. systemd hands the files to the service
  (`LoadCredential`, originals keep their permissions); ports below 1024 get
  `CAP_NET_BIND_SERVICE` only; `werkbank-tls.path` restarts the service when
  the certificate is renewed (also when symlinks are replaced).
- The app sets the security headers itself (CSP, X-Frame-Options,
  X-Content-Type-Options, Referrer-Policy; HSTS for https), identical to the
  Caddy site; no `Server` header.

### Changed
- `deploy/uninstall.sh` also removes the certificate units and drop-ins.

## [1.11.0] - 2026-10-07

### Changed
- License changed from GNU AGPL-3.0-only to GNU GPL-3.0-only (`LICENSE`,
  `pyproject.toml`, README).

## [1.10.3] - 2026-10-07

### Changed
- Examples in docs, installer help and tests use the placeholder
  "Max Mustermann" (`mmustermann`) instead of a real name; package authors
  are "Azubi-Werkbank contributors".

## [1.10.2] - 2026-10-07

### Fixed
- Installer: `caddy validate` (run as root) created
  `/var/log/caddy/werkbank.log` as root:root 0600, so the running Caddy could
  not open it, rejected the new configuration and `systemctl reload caddy`
  hung. The log file is now created for the `caddy` user before and after
  validation (an existing root-owned file is fixed on re-run).
- Installer: the Caddy reload has a time limit; if it fails, the `import` line
  is taken back so a later Caddy restart cannot fail because of it. The
  fallback `systemctl restart caddy` was removed – a failing restart would
  stop every site on a shared Caddy.

## [1.10.1] - 2026-10-07

### Fixed
- Behind Caddy, forms (setting the first password, login, password change)
  were rejected with "Die Sitzung ist abgelaufen …": the site sent
  `Referrer-Policy: no-referrer`, which makes browsers send `Origin: null` on
  form POSTs. The Caddy site now uses `Referrer-Policy: same-origin` (still no
  referrer to other sites). Existing installs: run `deploy/install.sh` again
  or change the header in `/etc/caddy/werkbank.caddy`.

## [1.10.0] - 2026-10-07

### Changed
- `deploy/install.sh` asks for the local port of the web server (default
  8000, or the port of an existing config). Ports below 1024 or already used
  by another program are rejected (asked again interactively, error with
  `--yes`). An existing config keeps its port.

## [1.9.0] - 2026-10-07

### Added
- `deploy/uninstall.sh`: removes service, `werkbank` command, `/etc/werkbank`,
  Caddy site and its `import` line (Caddyfile backed up, validated, restored
  on failure; other sites untouched), fail2ban files, cron files, venv and
  system user. Data is kept unless `--purge-data` is given; `--backup FILE`
  writes a `.tar.gz` of data and config first; `--remove-code` deletes the
  clone; `--data-dir` purges data kept by an earlier run. Safe to run again.
- Public demo workbook `workbooks/linux-basics.yaml`.

### Changed
- The workbook template uses neutral example content.
- Tests no longer need private catalogs (reference tests skip without them).
- README and deployment guide point to github.com/MajorMaxdom/azubi-werkbank.

## [1.8.1] - 2026-10-06

### Added
- License: GNU AGPL-3.0-only (`LICENSE`, `pyproject.toml`).
- README rewritten for GitHub: overview, workflow, features by role,
  installation, minimal catalog example (validated by a test), development,
  license; short German summary.

## [1.8.0] - 2026-10-06

### Added
- `deploy/install.sh`: one-step installation from a clone — asks for (or
  takes as options) data directory, domain and first Fachbetreuer; installs
  packages, system user, venv, config, systemd service, a `werkbank` command
  (`/usr/local/bin/werkbank`, runs as the service user) and optionally Caddy;
  prints the invite link. Safe to run again (keeps config, data and users).
  Refuses code/data below `/home`, `/root` or `/tmp`.

### Changed
- Caddy is integrated without replacing an existing configuration: the site
  lives in `/etc/caddy/werkbank.caddy`, the main Caddyfile only gets an
  `import` line (backed up; restored automatically if validation fails).
  `deploy/README.md` describes the same for manual installs.

## [1.7.0] - 2026-10-06

### Added
- Delete a workbook in the editor ("Löschen …" in the workbook list):
  confirmation page with the impact (apprentices with answers, assigned
  users, images) that requires typing the workbook id. The catalog file is
  moved to `workbooks/_backups/deleted/` (restore by moving it back),
  references in `users.yaml` are removed (an emptied workbook list stays
  `[]` = no workbook, comments kept). Answers and images are kept unless the
  corresponding checkbox is ticked.
- `/admin/users` shows "keine" for apprentices without any workbook.

 - 2026-10-06

### Changed
- The product is called **Azubi-Werkbank**: top bar, page titles
  (`<page> · Azubi-Werkbank`), README and docs.
- Technical short name `werkbank`: CLI command `werkbank` (was `workbook`),
  Python distribution `azubi-werkbank`, systemd unit `deploy/werkbank.service`,
  system user/group `werkbank`, paths `/opt/werkbank`, `/etc/werkbank`,
  `/var/lib/werkbank`, fail2ban filter/jail `werkbank`, example domain
  `werkbank.example.de`.
- Config environment variable `WERKBANK_CONFIG`; `WORKBOOK_CONFIG` is still
  read as a fallback.
- Unchanged on purpose: the catalog term "workbook" in code, the `workbooks/`
  directory, the `X-Workbook` API header and export file names.

 - 2026-10-06

### Fixed
- Dates and times were shown in the server's time zone (usually UTC, i.e.
  two hours off in German summer time; dates around midnight on the wrong
  day). New config key `timezone` (default `Europe/Berlin`) is used for the
  UI, CSV files, exports and download file names.
- The per-IP login limiter kept an entry for every IP forever; old entries
  are now forgotten.
- The form editor wrote `optional: false`, `multiple: false` and
  `monospace: false` into the YAML after toggling a checkbox; default values
  are omitted again.
- After an answer field changed its type in the catalog (e.g. text →
  checklist), old saved values could tick checkboxes by substring match or
  appear as `['…']` in a text field; values that no longer fit the type are
  ignored when rendering (they stay in the file).

 - 2026-10-06

### Added
- Image housekeeping per workbook (`/admin/editor/{workbook}/assets`, link
  "Bilder verwalten" in the editor): every image with thumbnail, size and
  where it is used (image blocks and any mention in task text); delete
  single unused images or all unused images at once. Images in use cannot be
  deleted.

### Changed
- The editor saves via `POST /api/editor/{workbook}` and deletes images via
  `POST …/assets/{name}/delete` — state-changing requests are POST/PATCH only,
  as required by the security rules (previously PUT/DELETE).
- `preview` and `strings` are reserved and cannot be used as workbook ids.
- `CLAUDE.md`: both roles may append messages to a task's question thread.

 - 2026-10-06

### Added
- **Mein Konto** (`/account`): change your own password (current password
  required; wrong attempts count towards the lockout; other sessions end,
  the current one stays) and download your own data.
- **Delete users for good** (GDPR): confirmation page that lists everything
  that will be removed and requires typing the username; removes the
  users.yaml entry (comments kept), credentials, all progress files and every
  Fachbetreuer assignment pointing to the user. Data export as ZIP
  (`user.json`, raw progress files, HTML export per workbook) for
  Fachbetreuer (`/admin/users/{username}/data`) and for yourself.
- **Image upload in the form editor**: upload PNG/JPEG/GIF/WebP (file
  signature checked, max. 5 MB, safe file names, never overwrites), pick
  existing images of the workbook, thumbnail preview.
- **Questions per task** ("Rückfragen"): apprentice and Fachbetreuer
  exchange messages on a task; open questions show up as "Rückfrage" in
  "Meine Aufgaben", count as "zu prüfen" and appear in "Nur zu prüfen".
- **Review history**: earlier reviews are kept and shown (review view,
  apprentice view, export). Comment-only edits by the same Fachbetreuer
  within 10 minutes update the current review instead of adding entries.
- **Bulk Fachbetreuer assignment** (`/admin/assign`): set the Fachbetreuer
  of a workbook and/or per module for many apprentices in one step, with
  "Alle Azubis auswählen".
- **CSV export** of the overview (`/admin/overview.csv`) and of
  "Meine Aufgaben" (`/my-tasks.csv`, same filters), formatted for German
  Excel and protected against formula injection.

### Security
- New credentials start at a random session version, so a cookie of a
  deleted account can never be accepted for a re-created account with the
  same username.

 - 2026-10-06

### Added
- "Meine Aufgaben" — Fachbetreuer filter, combinable with the status
  filters. Apprentices see it as soon as more than one Fachbetreuer is
  involved in their workbooks.
- Fachbetreuer can switch between "Meine" and "Alle Fachbetreuer" (all tasks
  of all active apprentices, e.g. to stand in for a colleague) and filter by
  Fachbetreuer, including "ohne Fachbetreuer".

 - 2026-10-06

### Added
- "Meine Aufgaben": filters "Geprüft – OK" and "Nacharbeiten" next to
  "Alle" and "Nur zu prüfen" / "Offen und Nacharbeiten".

 - 2026-10-06

### Added
- Form editor for Fachbetreuer (`/admin/editor`, "Editor" in the top bar):
  create workbooks (blank or as a copy) and edit every part — metadata,
  header fields, colours, levels, days, modules and tasks with answer fields,
  free blocks, hints, bonus and trainer content; add/move/remove items;
  per-task preview.
- Saving validates the whole catalog (errors are listed and marked at the
  field), backs up the previous file to `workbooks/_backups/` (last 10),
  writes YAML while keeping comments and formatting of unchanged parts, and
  refuses to overwrite a file that changed meanwhile.
- Id protection: ids of tasks and answer fields with saved answers are
  read-only; removing them requires an explicit confirmation.

### Changed
- `network-security.yaml`: two over-long lines re-wrapped (content
  unchanged) so that saving it in the editor produces no diff.

 - 2026-10-06

### Added
- "Meine Aufgaben" (`/my-tasks`, link in the top bar) for every logged-in
  user. Apprentices see every task of their workbooks with status (offen,
  erledigt – wartet auf Prüfung, geändert – erneut prüfen, geprüft – OK,
  Nacharbeiten), done date, review comment and responsible Fachbetreuer.
  Fachbetreuer see every task they are responsible for across all
  apprentices; filter "Nur zu prüfen".

 - 2026-10-06

### Added
- Export `GET /workbooks/{workbook}/export` (button "Export / Drucken"): a
  self-contained HTML snapshot with answers, done dates, review results and
  sign-off; fonts, tokens and the workbook theme are inlined, hints and bonus
  expanded, so it opens offline and prints cleanly. Fachbetreuer export any
  apprentice with `?user=` (including expectations and review details) or a
  blank workbook.
- `workbook schema -o FILE` exports the JSON Schema of the catalog format
  with a description for every key; `docs/workbook.schema.json` and
  `.vscode/settings.json` give autocompletion and live checks in VS Code.
- `docs/AUTHORING.md`: how to write a workbook; top-level `README.md`.
- Accessibility: skip link, labels for every form control (including
  answers without a label and per-task review fields), module LED summaries
  with the real count, reduced-motion support, visible focus everywhere.

### Changed
- Small coloured text (level badges, trainer heading, error messages) and
  placeholders are slightly darkened via `color-mix` with `--ink` so every
  default colour combination meets WCAG AA (4.5:1); tokens are unchanged.

 - 2026-10-06

### Added
- `deploy/Caddyfile` (TLS, HSTS, CSP and the other security headers from the
  plan, JSON access log), `deploy/workbook.service` (dedicated `workbook`
  user, state in `/var/lib/workbook`, `ProtectSystem=strict` and further
  hardening), optional fail2ban filter and jail.
- `deploy/README.md`: installation on Ubuntu/Debian, DNS and port
  forwarding, first Fachbetreuer via CLI, catalog authoring rights, backups
  (restic or nightly git commit), updates, troubleshooting.
- `config.example.yaml` with every configuration key.
- Tests: every server-rendered page is CSP-compatible (no inline scripts or
  styles, no event handlers, only same-origin scripts and stylesheets);
  deploy files contain the planned headers and hardening.

### Verified
- Install steps executed on Debian 12 with a hardened systemd service behind
  Caddy (internal TLS): invite, login, autosave, SSE through the proxy, real
  client IP in `auth.login_failed`, file modes 0600/0700.

 - 2026-10-06

### Added
- Review view `/workbooks/{workbook}/users/{username}` for Fachbetreuer:
  apprentice answers read-only, review status (— / OK / Nacharbeiten) and
  comment autosaved per task, responsible Fachbetreuer per task, last
  reviewer and time, "Verwaiste Antworten" for answers without a catalog
  field, sign-off (comment and date; "Geprüft durch" is the acting
  Fachbetreuer from the session).
- JSON API `PATCH …/users/{username}/tasks/{task}/review` and
  `PATCH …/users/{username}/signoff` (Fachbetreuer only; apprentices get 403).
- `/admin/overview`: matrix apprentices × tasks per workbook with port LEDs
  (open, done, checked OK, Nacharbeiten), done counts, open checks and the
  responsible Fachbetreuer; every LED links to the task in the review view.
- Start page for Fachbetreuer: "Meine Azubis" — every apprentice workbook
  they are responsible for (fully or for single tasks) with progress and
  open checks. A check re-opens when the apprentice changes a task after
  the review.
- Apprentices see review status, comment and sign-off read-only; trainer
  expectations and notes never reach their HTML (tested on the full
  network-security workbook).

 - 2026-10-06

### Added
- Progress store `progress/<workbook>/<user>.json` (atomic writes under a
  per-file lock, mode 0600 / dirs 0700, ids validated before use in paths;
  unreadable files are never overwritten and show a German error page).
- JSON API `PATCH /api/progress/{workbook}/header` and
  `PATCH /api/progress/{workbook}/tasks/{task}` (`{answers?, done?}`): only
  for the apprentice's own progress; Fachbetreuer get 403. Unknown task or
  answer ids and values that do not fit the answer type are 422; answers are
  limited to 20 000 characters, requests to 1 MB (413).
- Autosave (vanilla JS): 800 ms debounce, status "Gespeichert" / "Speichert …"
  / "Offline – Änderungen werden erneut gesendet", retry with backoff, warning
  on leaving with unsaved changes, flush when the tab is hidden.
- Saved state is rendered on reload; live progress bar and port LEDs; own
  progress on the start page; hint "Diese Aufgabe wurde geändert, nachdem du
  sie bearbeitet hast." when the task content hash changed.
- Answers of removed tasks stay in the file and are not rendered or counted.
- Hints and bonus sections are expanded when printing.
- Fachbetreuer see the workbook as a read-only preview.

## [0.3.1] - 2026-10-06

### Added
- `/admin/users/{username}`: edit an apprentice in the browser — name,
  allowed workbooks, Fachbetreuer per workbook and per-task overrides.

## [0.3.0] - 2026-10-06

### Added
- Login/logout with signed session cookies (`__Host-session`, HttpOnly,
  Secure, SameSite=Strict), 8 h idle timeout with `last_seen` refresh at most
  every 5 minutes; sessions end immediately on reset, deactivation or removal.
- `users.yaml` model, hot reload and round-trip writes that keep comments;
  `data/credentials.json` and `data/secret.key` (mode 0600, dir 0700) with
  atomic writes under per-file locks shared by server and CLI.
- argon2id passwords (min. 12 characters); identical German error for every
  failed login; lockout after 5 failures per user for 15 minutes; in-memory
  limit of 20 failures per IP in 15 minutes (X-Forwarded-For trusted only from
  loopback); log line `auth.login_failed user=<name> ip=<ip>`.
- Invites: 32 random bytes, only the SHA-256 stored, single use, valid 72 h.
- `/admin/users`: create (username suggestion), reset access, deactivate,
  activate, copy invite link; status per user; warnings for the Fachbetreuer
  assignment (`supervisors` per apprentice workbook with per-task overrides).
- CSRF protection for forms (same-origin `Origin` + token) and the helper for
  the JSON API (`X-Workbook: 1`); German error pages.
- CLI: `workbook user add | reset | list`.
- Every page, theme, asset and the event stream requires login; `/admin/*`
  requires the `trainer` role; apprentices only see their workbooks.

### Changed
- Config key `secure_cookies` (default `true`) for plain-HTTP testing.
- Catalog images are served by `/assets/{path}` behind the login.

### Added (between 0.2.0 and 0.3.0)
- Answer option `monospace: true` (types `text` and `short`) for tables,
  protocols and command output.

### Changed (between 0.2.0 and 0.3.0)
- UI term "Ausbilder" is now "Fachbetreuer" (role value stays `trainer`).
- `network-security` workbook 4.1.0: "Ausbilder" → "Fachbetreuer" in the
  content; header field "Ausbilder/in" removed.
- `network-security` workbook 4.2.0: intro and footer describe autosave
  instead of the old "Stand speichern" download; 13 answer fields are
  monospace again, as in the reference.
- `docs/PLAN.md`: Fachbetreuer assignment per apprentice workbook with
  per-task overrides (implemented with the `users.yaml` model in 0.3.0).

## [0.2.0] - 2026-10-06

### Added
- FastAPI web server (`workbook serve`), configuration via `config.yaml`
  (all keys optional, relative paths resolved against the config file).
- Catalog registry with hot reload (watchfiles): new, changed and deleted files
  are picked up without restart; a broken file keeps its last valid version;
  duplicate workbook ids across files resolve deterministically (first file
  by name wins).
- Pages: start page with all workbooks, workbook view rendered from the
  registry, `/admin/catalogs` with German error messages, English key paths,
  line numbers and WCAG contrast warnings for workbook stylesheets.
- `GET /workbooks/{id}/theme.css` with `ETag` / `If-None-Match` and
  `Cache-Control: no-cache`; linked as `theme.css?v=<hash>` after `tokens.css`.
- Server-Sent Events (`GET /events`): open workbook pages show the banner
  "Dieses Heft wurde aktualisiert – neu laden" instead of reloading.
- Catalog images are served from `workbooks/assets/` under `/assets/`.

### Changed
- Theme generation moved to `app/theme.py`; the catalog registry lives in
  `app/loader.py`.

### Notes
- No authentication yet: workbook pages render the apprentice view and
  `/admin/catalogs` is open. Keep the server bound to `127.0.0.1` until 0.3.0.

## [0.1.0] - 2026-10-06

### Added
- Project skeleton (`pyproject.toml`, ruff config, venv-based install).
- Pydantic v2 catalog models with all validation rules from `docs/PLAN.md`
  section 2 (ID regex and uniqueness, module codes, level references,
  `blocks` exclusivity, answer type requirements, duration and semver formats,
  safe image paths, per-workbook `stylesheet` with hex-only colors).
- Catalog loader for YAML/JSON with key paths and line numbers in errors;
  workbook ids must be unique across files; files starting with `_` are ignored.
- Renderer: Markdown (raw HTML disabled), duration parsing, day load
  ("3 Aufgaben · ~2,5 h"), automatic task numbering, task content hash,
  generated theme CSS.
- Jinja2 templates, design tokens (`tokens.css`), component styles and print
  stylesheet; self-hosted Inter / JetBrains Mono and Lucide icons.
- German UI strings in `locales/de.yaml` with the `t()` helper.
- CLI: `workbook validate [PATH]` and `workbook render FILE -o OUT.html`
  (self-contained preview with tokens, theme and fonts inlined).
- `workbooks/network-security.yaml`, converted from the reference workbook
  (6 days, 9 modules, 21 tasks, all hints, bonus sections and trainer
  expectations); `workbooks/_template.yaml`.
- Tests for every validation rule, duration/load calculation, theme generation,
  design/language rules and content completeness against the reference.
