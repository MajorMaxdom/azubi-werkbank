# Azubi-Werkbank

**Interactive workbooks for IT apprentices — self-hosted, no database.**

Azubi-Werkbank turns human-readable YAML task catalogs ("Arbeitshefte") into
interactive web pages. Apprentices work through tasks in the browser, every
answer is saved automatically, and their Fachbetreuer (trainers) review each
task, leave feedback and sign the workbook off. Catalogs are plain files: edit
them in the browser or in your text editor, and changes appear within a second.

> **Auf Deutsch:** Azubi-Werkbank ist ein selbst gehosteter Webserver für
> Arbeitshefte in der IT-Ausbildung. Azubis bearbeiten Aufgaben im Browser,
> alles wird automatisch gespeichert. Fachbetreuer bewerten jede Aufgabe
> (OK / Nacharbeiten), beantworten Rückfragen und nehmen das Heft ab. Hefte
> werden als YAML-Datei oder im Formular-Editor erstellt. Die Oberfläche ist
> komplett deutsch. Installation auf einem Debian/Ubuntu-Server mit einem
> Skript: siehe [Installation](#installation).

<!--
Screenshots: add images to docs/screenshots/ and reference them here, e.g.
![Workbook view](docs/screenshots/workbook.png)
![Review view](docs/screenshots/review.png)
-->

## How it works

1. A Fachbetreuer writes a workbook — days, modules and tasks with hints,
   answer fields and expectations — in the form editor or as a YAML file.
2. Apprentices are invited by link, set their own password and see the
   workbooks assigned to them.
3. Apprentices answer tasks and tick them off. Saving is automatic, works
   through short network outages and shows its state at all times.
4. The responsible Fachbetreuer reviews each task (OK / Nacharbeiten),
   answers questions in the task's thread and finally signs the workbook off.
5. Everything can be exported as a self-contained, printable HTML file.

## Features

**For apprentices**
- Clean worksheet layout with collapsible hints and bonus tasks, progress bar
  and per-module progress LEDs
- Autosave with status indicator and offline retry
- Feedback and questions per task; "Meine Aufgaben" lists every task with its
  status and Fachbetreuer
- Own data export (ZIP) and password change

**For Fachbetreuer (trainers)**
- Review view per apprentice with expectations ("Das sollte drinstehen"),
  review history and sign-off
- "Meine Azubis" start page, overview matrix of all apprentices × tasks,
  filters (to review, OK, Nacharbeiten, by Fachbetreuer), CSV export
- Fachbetreuer per apprentice workbook with per-task overrides, also in bulk
  for many apprentices at once
- User management with invite links, deactivation and GDPR deletion

**For workbook authors**
- Form editor in the browser with preview, validation at the field, id
  protection for tasks that already have answers, image upload and cleanup
- Or plain YAML/JSON files with a JSON Schema for autocompletion in VS Code;
  errors are reported with key path and line number
- Hot reload: new and changed files are live within a second; a broken file
  keeps its last valid version
- Per-workbook colour themes with contrast warnings

**Operations and security**
- Plain files instead of a database: easy to back up, inspect and version
- Invite links, argon2id passwords, lockout and rate limiting, CSRF/Origin
  checks, strict Content-Security-Policy, sandboxed systemd service
- Trainer-only content never reaches apprentices' browsers
- One-step installer for Debian/Ubuntu behind Caddy with automatic HTTPS

## Installation

Requirements: Debian 12 or Ubuntu 24.04, Python 3.11+, root access. For
HTTPS: a domain pointing to the server and ports 80/443 reachable.

```sh
sudo git clone https://github.com/<account>/azubi-werkbank.git /opt/werkbank
cd /opt/werkbank
sudo ./deploy/install.sh
```

The installer asks for the data directory, the domain and the first
Fachbetreuer, sets up everything (system user, service, Caddy) and prints the
invite link. It is safe to run again and leaves an existing Caddy setup with
other sites intact. Manual steps, backups and updates:
[`deploy/README.md`](deploy/README.md).

## Writing a workbook

A minimal catalog:

```yaml
schema_version: 1
workbook:
  id: network-basics
  version: 1.0.0
  title: "Arbeitsheft: Netzwerkgrundlagen"
levels:
  understand: {label: Verstehen, color: blue}
days:
  - id: day-1
    title: Ankommen
    modules:
      - code: NET-01
        title: IP-Adressen
        tasks:
          - id: net01-subnet
            title: Subnetzmaske erklären
            level: understand
            duration: 30m
            requirement: Erkläre in eigenen Worten, wozu eine Subnetzmaske dient.
            hints: Denk an eine Postleitzahl.
            answers:
              - {id: answer, label: "Deine Erklärung:"}
            trainer:
              expectations: [Netz- und Hostanteil werden unterschieden.]
```

Every field is documented in [`docs/workbook-template.yaml`](docs/workbook-template.yaml);
the guide is [`docs/AUTHORING.md`](docs/AUTHORING.md). A small demo workbook
ships in [`workbooks/linux-basics.yaml`](workbooks/linux-basics.yaml).

## Development

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/werkbank user add mmustermann --name "Max Mustermann" --role trainer   # prints an invite link
.venv/bin/werkbank serve                                        # http://127.0.0.1:8000
.venv/bin/ruff check . && .venv/bin/ruff format --check . && .venv/bin/pytest
```

To test over plain HTTP from another machine, create `config.yaml` (see
[`config.example.yaml`](config.example.yaml)) with `listen_host`, a matching
`base_url` and `secure_cookies: false`.

| Command | Purpose |
|---|---|
| `werkbank serve` | run the server |
| `werkbank validate [PATH]` | validate catalogs (exit code 1 on errors) |
| `werkbank render FILE -o OUT.html` | offline preview of a catalog |
| `werkbank schema -o FILE` | JSON Schema for editor autocompletion |
| `werkbank user add / reset / list` | manage users and invite links |

**Tech stack:** Python 3.11+, FastAPI, Jinja2, Pydantic v2, ruamel.yaml,
watchfiles, argon2-cffi; server-rendered HTML with vanilla JavaScript — no
frontend framework, no build step, no CDN.

```
app/          application (models, loader, auth, progress, editor, API, templates, static)
cli.py        `werkbank` command line
workbooks/    catalogs (*.yaml, *.json) and assets/
locales/      German UI strings
deploy/       installer, systemd unit, Caddyfile, fail2ban, deployment guide
docs/         specification, authoring guide, template, JSON Schema
tests/        pytest suite
```

## Documentation

- [`docs/AUTHORING.md`](docs/AUTHORING.md) — writing workbooks (editor and YAML)
- [`docs/workbook-template.yaml`](docs/workbook-template.yaml) — every catalog field
- [`deploy/README.md`](deploy/README.md) — installation, backups, updates
- [`docs/PLAN.md`](docs/PLAN.md) — specification
- [`CHANGELOG.md`](CHANGELOG.md) — release history

## License

Azubi-Werkbank is licensed under the
[GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0-only). If you run
a modified version as a network service, you must offer its source code to
its users.

Bundled third-party assets: Inter and JetBrains Mono fonts (SIL Open Font
License 1.1) and Lucide icons (ISC License) — license texts in
`app/static/fonts/` and `app/static/icons/`.
