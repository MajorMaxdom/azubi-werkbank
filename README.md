# Azubi-Werkbank

**Azubi-Werkbank** is a small self-hosted web server that turns YAML/JSON task catalogs
("Arbeitshefte") for IT apprentices into interactive German web pages.
Apprentices log in, answer tasks and tick them off — everything is saved
automatically. Their Fachbetreuer (trainers) review each task, comment, and
sign off the workbook. No database, no Docker, no frontend build step.

## Features

- Catalogs as human-readable YAML/JSON, validated on load and hot-reloaded;
  broken files keep their last valid version.
- Per-user progress files with autosave, offline retry and live progress LEDs.
- Review view, "Meine Azubis" start page and an overview matrix for
  Fachbetreuer; per-task Fachbetreuer assignment.
- Invite links, argon2id passwords, rate limiting, CSRF/Origin checks,
  strict Content-Security-Policy.
- Self-contained HTML export of a workbook with answers (offline, printable).
- Per-workbook colour themes; light, print-friendly design.

## Installation

On a Debian/Ubuntu server: clone to `/opt/werkbank` and run
`sudo ./deploy/install.sh` — details in [`deploy/README.md`](deploy/README.md).

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/werkbank validate                 # check workbooks/
.venv/bin/werkbank user add mmustermann --name "Max Mustermann" --role trainer
.venv/bin/werkbank serve                    # http://127.0.0.1:8000
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
```

For plain-HTTP testing on another host, create `config.yaml` (see
`config.example.yaml`) with `listen_host`, a matching `base_url` and
`secure_cookies: false`.

## Commands

| Command | Purpose |
|---|---|
| `werkbank serve` | run the server |
| `werkbank validate [PATH]` | validate catalogs (exit code 1 on errors) |
| `werkbank render FILE -o OUT.html` | offline preview of a catalog |
| `werkbank schema -o FILE` | JSON Schema for editor autocompletion |
| `werkbank user add/reset/list` | manage users and invite links |

## Documentation

- [`docs/AUTHORING.md`](docs/AUTHORING.md) — how to write a workbook
- [`docs/workbook-template.yaml`](docs/workbook-template.yaml) — every catalog field
- [`deploy/README.md`](deploy/README.md) — installation behind Caddy with systemd
- [`docs/PLAN.md`](docs/PLAN.md) — specification
- [`CHANGELOG.md`](CHANGELOG.md)

Fonts: Inter and JetBrains Mono (SIL OFL 1.1); icons: Lucide (ISC) — see the
license files under `app/static/`.
