# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
versions follow [Semantic Versioning](https://semver.org/).

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
