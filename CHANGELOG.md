# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Answer option `monospace: true` (types `text` and `short`) for tables,
  protocols and command output.

### Changed
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
