# Writing a workbook

A workbook ("Arbeitsheft") is one YAML (or JSON) file in the `workbooks/`
directory. The server picks up new and changed files within about a second —
no restart, no build step. This guide explains how to write one. The complete
field reference with comments is [`workbook-template.yaml`](workbook-template.yaml);
a real, complete example is `workbooks/network-security.yaml`.

Keys are English; everything apprentices read (titles, tasks, hints, labels)
is written in German.

## Form editor (in the browser)

Fachbetreuer can create and edit workbooks without touching YAML: **Editor**
in the top bar (`/admin/editor`).

- **New workbook**: title + id, either blank (one day, one module, one task to
  start from) or as a copy of an existing workbook.
- **Editing**: metadata, header fields, colours, levels, and the tree of days,
  modules and tasks. Every item can be added, moved up/down and removed; a
  task opens into a form with all fields (fixed layout or free blocks,
  answer fields, hints, bonus, "Das sollte drinstehen", notes) and a
  **Vorschau** button that renders it exactly as Fachbetreuer see it.
- **Images**: an `image` block has a picker with the images already stored
  for this workbook, a thumbnail of the current `src`, and an upload field
  (**Hochladen**). Uploads go to `workbooks/assets/<workbook-id>/` and set
  `src` automatically. Only PNG, JPEG, GIF and WebP up to 5 MB are accepted
  (checked by the file content, not the name; no SVG). File names are
  lowercased and transliterated (`Größe.PNG` → `groesse.png`); an existing
  file is never overwritten (`-2`, `-3` … is appended). The image is part of
  the workbook only after **Speichern**. **Bilder verwalten** (link at the top
  of the editor) lists every image of the workbook with thumbnail, size and
  where it is used; unused images can be deleted one by one or all at once.
  Images that an image block or any task text still refers to cannot be
  deleted.
- **Saving** validates the whole workbook first; problems are listed and
  marked at the field. The previous file is copied to
  `workbooks/_backups/` (last 10 versions per workbook), then the YAML is
  rewritten — comments and formatting of unchanged parts are kept.
- **Id protection**: ids of tasks and answer fields that already have saved
  answers are read-only (marked "hat Antworten"). Removing such a task or
  field asks for confirmation; the saved answers stay in the progress files
  ("Verwaiste Antworten").
- **Deleting** a workbook ("Löschen …" in the workbook list) asks for the
  workbook id as confirmation. The file is moved to
  `workbooks/_backups/deleted/` and can be restored by moving it back
  (assignments to apprentices have to be set again). Saved answers and
  images are kept unless you tick the boxes to delete them too.
- If the file was changed elsewhere in the meantime (by hand or by another
  Fachbetreuer), saving is refused instead of overwriting — reload the page.

Everything below describes the file format itself, for authors who prefer
writing YAML directly.

## Quick start

```sh
cp docs/workbook-template.yaml workbooks/my-topic.yaml   # in production: /var/lib/werkbank/workbooks/
# edit: workbook.id, title, levels, days ...
werkbank validate workbooks/my-topic.yaml                 # exit code 1 on errors
werkbank render workbooks/my-topic.yaml -o preview.html   # offline preview incl. trainer area
```

Then open `/admin/catalogs` in the browser: every file is listed with its
status, errors (with key path and line number) and colour contrast warnings.
Files whose name starts with `_` (like `_template.yaml`) are ignored by the
server — handy for drafts.

## Structure

```yaml
schema_version: 1
workbook:        # metadata: id, version, title, intro, header fields, colours ...
levels:          # difficulty levels, referenced by tasks
days:            # days -> modules -> tasks
  - id: day-1
    title: ...
    modules:
      - code: CRT-01
        title: ...
        tasks:
          - id: sys01-boot
            title: ...
            level: understand
            duration: 45m
            requirement: ...
            answers: [...]
```

- **Days** become the coloured banners and the quick navigation. Their load
  ("3 Aufgaben · ~2,5 h") is calculated from the task durations.
  `optional: true` marks a voluntary day (e.g. a final project, `nav_tag: OPT`).
- **Modules** are the cards with code, title, learning objective and one port
  LED per task.
- **Tasks** are numbered automatically as `<day>.<position within the day>`;
  set `number: "4.1"` to number them differently (e.g. per module). Quote
  numbers, otherwise YAML reads `4.1` as a decimal.

## IDs — the one rule that matters

`workbook.id`, day ids, task ids, answer ids and header field ids use
lowercase `a-z`, `0-9` and `-` (max. 63 characters) and are unique in their
scope. **Progress is stored by id.** Never change or reuse the id of a task or
answer once apprentices have started — change titles and texts instead, which
is always safe. A good convention is `<module>-<topic>`, e.g. `lb02-failover`.

## Writing a task

A task uses either the **fixed layout** or **free blocks**.

Fixed layout — rendered in this order:

| Field | Shown as |
|---|---|
| `requirement` | the assignment (Markdown), labelled "Anforderung" |
| `snippet` | monospace block, verbatim (sample output, diagrams) |
| `steps` | numbered click path, one Markdown item each |
| `guiding_questions` | list with "?" markers |
| `hints` | collapsible "Hilfestellung anzeigen" (Markdown) |
| `answers` | input fields (see below) |
| done checkbox | always present |
| `bonus` | collapsible "Extra, wenn du früher fertig bist" (Markdown) |
| `trainer` | only for Fachbetreuer (see below) |

Free layout — `blocks` replaces `requirement`, `snippet`, `steps` and
`guiding_questions` (using both is an error). Block types: `text`, `table`,
`snippet`, `steps`, `questions`, `image`, `note` (`variant: info | warning`).
`hints`, `answers`, `bonus` and `trainer` work the same in both layouts.

### Answer fields

| `type` | Input | Extra keys |
|---|---|---|
| `text` (default) | multi-line text area | `height` (px), `placeholder`, `monospace` |
| `short` | one line | `placeholder`, `monospace` |
| `date` | date picker | — |
| `checklist` | checkboxes | `items` (required) |
| `choice` | radio buttons, or checkboxes with `multiple: true` | `options` (required) |

Use `monospace: true` for tables, protocols and command output. Placeholders
may span several lines (use a `|-` block) to show the expected structure,
e.g. `"1. Buchstabe ___ : ..."` per line.

### Trainer content

```yaml
trainer:
  expectations:            # "Das sollte drinstehen" — bullet list
    - Reihenfolge A – C – E – D – B.
  notes: |                 # internal notes
    Gut geeignet für ein kurzes Gespräch danach.
```

Only Fachbetreuer see this — in their preview, the review view and their
export. It is never sent to apprentices, not even hidden in the HTML.
Changing trainer content does not trigger the "task changed" hint.

### Hints and bonus

Write hints so that apprentices can get unstuck without being handed the
solution: an analogy ("Bild im Kopf"), where to look, search terms. Bonus
sections are voluntary deepening for fast learners.

## Markdown and YAML tips

- Markdown fields support `**bold**`, `*italic*`, `` `code` ``, lists, links
  and fenced code blocks. **Raw HTML is not allowed** — it is shown as text.
- Use block scalars for longer text: `|` keeps line breaks (Markdown joins
  lines of a paragraph anyway), `|-` drops the final newline.
- Quote values that contain `: `, start with special characters, or look like
  numbers or booleans: `title: "Wiederholung: DNS"`, `number: "1.1"`,
  `nav_tag: "1"`.
- Colours must be quoted: `accent: "#1B365D"` — unquoted, `#` starts a comment.
- Indent with spaces only. Typos in keys are errors (`titel:` instead of
  `title:` is reported with its line number).

## Images

Put images under `workbooks/assets/<workbook-id>/` and reference them relative
to `workbooks/`:

```yaml
- type: image
  src: assets/network-security/topology.png
  alt: Netzwerktopologie des Labors      # required
  caption: "Abb. 1: Labor-Topologie"
```

Paths outside `workbooks/assets/` are rejected.
The form editor can upload images into this folder for you (see above).

## Colours

`workbook.stylesheet` overrides the default colour scheme for this workbook
only. Every key is optional; missing keys keep the default. Only hex colours
are allowed. Setting just `accent` also derives matching hover and tint
colours. `/admin/catalogs` warns when a combination has a contrast below
4.5:1 (body text on page/card, white on accent, accent on card) — fix those
before publishing. Fonts, spacing and layout are not configurable.

## Changing a published workbook

- **Text changes** are live within a second. If the content of a task changes
  after an apprentice worked on it, the apprentice sees "Diese Aufgabe wurde
  geändert, nachdem du sie bearbeitet hast."
- **Broken file**: the last valid version stays online; the error is shown on
  `/admin/catalogs`. Open pages show "Dieses Heft wurde aktualisiert – neu
  laden" only for valid changes.
- **Removed tasks or answer fields**: saved answers are kept and shown to
  Fachbetreuer under "Verwaiste Antworten" in the review view; they are no
  longer counted in the progress.
- Bump `workbook.version` (semantic versioning) for every published change:
  patch for typos, minor for new or reworded tasks, major for restructuring.

## Editor setup (VS Code)

Install the extension **YAML** (`redhat.vscode-yaml`). The repository's
`.vscode/settings.json` maps `workbooks/*.yaml` and `docs/workbook-template.yaml`
to `docs/workbook.schema.json`, so you get autocompletion, hover descriptions
for every key and red underlines for unknown keys, wrong types and wrong
formats (ids, durations, versions, colours) while typing.

For files outside the repository (e.g. on the server), put this first line in
the catalog:

```yaml
# yaml-language-server: $schema=/opt/werkbank/docs/workbook.schema.json
```

Regenerate the schema after changing the models:
`werkbank schema -o docs/workbook.schema.json`. The schema checks structure
and formats; rules that span the whole file (unique ids, level references,
blocks vs. fixed fields) are checked by `werkbank validate`.

## Checklist before publishing

1. `werkbank validate` passes.
2. `werkbank render ... -o preview.html` looks right, including hints, bonus
   and trainer area.
3. No contrast warning on `/admin/catalogs`.
4. Every task has a `level`, a `duration` and at least one answer field.
5. Ids follow the module convention and will not need to change.
6. `workbook.version` is bumped.
