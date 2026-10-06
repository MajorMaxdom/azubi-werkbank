from __future__ import annotations

import json

import pytest
from ruamel.yaml import YAML

from app.editor import clean, editor_loc, merge, prepare, render_document
from tests.conftest import BASE_URL, ORIGIN, ROOT, add_user, csrf_from, make_config, open_client
from tests.conftest import second_client as second
from tests.test_progress import CATALOG

READ = {"X-Workbook": "1"}
WRITE = {"Origin": BASE_URL, "X-Workbook": "1"}

COMMENTED = "# Kopfkommentar bleibt\n" + CATALOG.replace(
    "          - id: t1\n", "          # Aufgabe eins – Kommentar bleibt\n          - id: t1\n"
)


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(COMMENTED, encoding="utf-8")
    add_user(config, "boss", "trainer", name="Chefin Boss")
    add_user(config, "anna", "apprentice", name="Anna Azubi")
    return config


def load(client) -> dict:
    response = client.get("/api/editor/demo", headers=READ)
    assert response.status_code == 200, response.text
    return response.json()


def put(client, data, base_hash, confirm=None):
    body = {"data": data, "base_hash": base_hash, "confirm_delete": confirm or {}}
    return client.put("/api/editor/demo", json=body, headers=WRITE)


# ------------------------------------------------------------------ load & save


def test_load_returns_plain_data_hash_and_locks(config):
    with open_client(config, "boss") as boss:
        loaded = load(boss)
    assert loaded["file"] == "demo.yaml"
    assert loaded["data"]["workbook"]["id"] == "demo"
    assert loaded["data"]["days"][0]["modules"][0]["tasks"][0]["answers"][0] == {
        "id": "a1",
        "label": "Text",
    }
    assert len(loaded["base_hash"]) == 64
    assert loaded["locked"] == {}


def test_strings_endpoint(config):
    with open_client(config, "boss") as boss:
        strings = boss.get("/api/editor/strings", headers=READ).json()
    assert strings["save_failed"] == "Speichern fehlgeschlagen."
    assert strings["add_task"] == "Aufgabe hinzufügen"


def test_save_keeps_comments_and_updates_registry(config):
    path = config.paths.workbooks / "demo.yaml"
    with open_client(config, "boss") as boss:
        loaded = load(boss)
        data = loaded["data"]
        data["days"][0]["modules"][0]["tasks"][0]["title"] = "Neuer Titel"
        data["days"][0]["modules"][0]["tasks"][0]["hints"] = "Zeile 1\nZeile 2"
        data["workbook"]["subtitle"] = ""  # empty optional values are dropped
        response = put(boss, data, loaded["base_hash"])
        assert response.status_code == 200, response.text
        assert response.json()["version"] == "1.2.0"
        assert "Neuer Titel" in boss.get("/workbooks/demo").text  # registry reloaded
        assert response.json()["base_hash"] == load(boss)["base_hash"]
    text = path.read_text(encoding="utf-8")
    assert text.startswith("# Kopfkommentar bleibt\n")
    assert "# Aufgabe eins – Kommentar bleibt" in text
    assert "title: Neuer Titel" in text
    assert "hints: |\n              Zeile 1\n              Zeile 2\n" in text
    assert "subtitle" not in text
    assert "{id: a1, label: Text}" in text  # untouched flow style survives


def test_validation_errors_have_paths_and_german_messages(config):
    with open_client(config, "boss") as boss:
        loaded = load(boss)
        data = loaded["data"]
        data["days"][0]["modules"][0]["tasks"][0]["duration"] = "45min"
        response = put(boss, data, loaded["base_hash"])
        assert response.status_code == 422
        errors = {e["path"]: e["message"] for e in response.json()["errors"]}
        assert errors["days.0.modules.0.tasks.0.duration"].startswith("Ungültige Dauer")
        # Cross-reference rules are reported once the structure is valid.
        data["days"][0]["modules"][0]["tasks"][0]["duration"] = "45m"
        data["days"][0]["modules"][0]["tasks"][1]["level"] = "nope"
        response = put(boss, data, loaded["base_hash"])
        errors = {e["path"]: e["message"] for e in response.json()["errors"]}
        assert errors["days.0.modules.0.tasks.1.level"].startswith("Unbekannte Stufe")
    assert (config.paths.workbooks / "demo.yaml").read_text() == COMMENTED


def test_workbook_id_cannot_change(config):
    with open_client(config, "boss") as boss:
        loaded = load(boss)
        loaded["data"]["workbook"]["id"] = "anders"
        response = put(boss, loaded["data"], loaded["base_hash"])
    assert response.status_code == 422
    assert response.json()["errors"][0]["message"] == "Die Heft-ID kann nicht geändert werden."


def test_conflict_when_file_changed(config):
    with open_client(config, "boss") as boss:
        loaded = load(boss)
        path = config.paths.workbooks / "demo.yaml"
        path.write_text(COMMENTED.replace("title: Demo", "title: Von Hand"), encoding="utf-8")
        response = put(boss, loaded["data"], loaded["base_hash"])
    assert response.status_code == 409
    assert response.json() == {"conflict": True}
    assert "Von Hand" in path.read_text()


def test_backups_are_created_and_limited(config):
    with open_client(config, "boss") as boss:
        for n in range(12):
            loaded = load(boss)
            loaded["data"]["workbook"]["title"] = f"Titel {n}"
            assert put(boss, loaded["data"], loaded["base_hash"]).status_code == 200
    backups = sorted((config.paths.workbooks / "_backups").glob("demo.*.yaml"))
    assert len(backups) == 10
    assert "Titel 10" in backups[-1].read_text()
    assert len(list(config.paths.workbooks.glob("*.yaml"))) == 1  # backups are not catalogs


# ------------------------------------------------------------------ id protection


def test_removing_tasks_with_answers_needs_confirmation(config):
    with open_client(config, "boss") as boss:
        anna = second(boss, "anna")
        anna.patch(
            "/api/progress/demo/tasks/t2", json={"answers": {"a1": "Antwort"}}, headers=WRITE
        )
        loaded = load(boss)
        assert loaded["locked"] == {"t2": ["a1"]}
        data = loaded["data"]
        del data["days"][0]["modules"][0]["tasks"][1]
        response = put(boss, data, loaded["base_hash"])
        assert response.status_code == 409
        assert response.json() == {"confirm": {"t2": []}}
        confirmed = put(boss, data, loaded["base_hash"], {"t2": []})
        assert confirmed.status_code == 200
        assert "Aufgabe zwei" not in boss.get("/workbooks/demo").text
    stored = json.loads((config.paths.progress / "demo" / "anna.json").read_text())
    assert stored["tasks"]["t2"]["answers"]["a1"] == "Antwort"  # kept as orphan


def test_removing_answer_fields_with_answers_needs_confirmation(config):
    with open_client(config, "boss") as boss:
        anna = second(boss, "anna")
        anna.patch("/api/progress/demo/tasks/t1", json={"answers": {"a1": "x", "dt": ""}},
                   headers=WRITE)  # fmt: skip
        loaded = load(boss)
        assert loaded["locked"] == {"t1": ["a1"]}  # empty answers do not lock
        data = loaded["data"]
        answers = data["days"][0]["modules"][0]["tasks"][0]["answers"]
        answers[0]["id"] = "renamed"
        response = put(boss, data, loaded["base_hash"])
        assert response.status_code == 409
        assert response.json() == {"confirm": {"t1": ["a1"]}}
        assert put(boss, data, loaded["base_hash"], {"t1": []}).status_code == 409
        assert put(boss, data, loaded["base_hash"], {"t1": ["a1"]}).status_code == 200


# ------------------------------------------------------------------ create & copy


def create(client, **form):
    token = csrf_from(client.get("/admin/editor").text)
    return client.post("/admin/editor", data={**form, "csrf_token": token}, headers=ORIGIN,
                       follow_redirects=False)  # fmt: skip


def test_create_blank_workbook(config):
    with open_client(config, "boss") as boss:
        response = create(boss, title="Neues Heft", id="neu")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/editor/neu"
        assert boss.get("/admin/editor/neu").status_code == 200
        assert "Neues Heft" in boss.get("/").text
    text = (config.paths.workbooks / "neu.yaml").read_text()
    assert text.startswith("# Workbook catalog — edited with the form editor")
    assert "id: neu" in text and "version: 1.0.0" in text


def test_create_copy(config):
    with open_client(config, "boss") as boss:
        assert create(boss, title="Kopie", id="kopie", source="demo").status_code == 303
        data = load(boss)  # still demo
        copy = boss.get("/api/editor/kopie", headers=READ).json()["data"]
    assert copy["workbook"]["title"] == "Kopie" and copy["workbook"]["version"] == "1.0.0"
    assert copy["days"] == data["data"]["days"]


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"title": "", "id": "x"}, "Bitte einen Titel angeben."),
        ({"title": "X", "id": "Ungültig!"}, "Ungültige Heft-ID"),
        ({"title": "X", "id": "demo"}, "schon vergeben"),
        ({"title": "X", "id": "neu", "source": "ghost"}, "Vorlage"),
    ],
)
def test_create_validation(config, form, message):
    with open_client(config, "boss") as boss:
        response = create(boss, **form)
    assert response.status_code == 400
    assert message in response.text


# ------------------------------------------------------------------ preview


def test_preview_renders_task_with_trainer_content(config):
    task = {"id": "x1", "title": "Vorschau-Aufgabe", "level": "u", "duration": "30m",
            "requirement": "**fett** <b>roh</b>", "answers": [{"id": "a", "label": "Feld"}],
            "trainer": {"expectations": ["Erwartung"]}}  # fmt: skip
    levels = {"u": {"label": "Verstehen", "color": "blue"}}
    with open_client(config, "boss") as boss:
        response = boss.post("/api/editor/preview", json={"task": task, "levels": levels},
                             headers=WRITE)  # fmt: skip
        assert response.status_code == 200
        html = response.json()["html"]
        assert "Vorschau-Aufgabe" in html and "<strong>fett</strong>" in html
        assert "&lt;b&gt;roh&lt;/b&gt;" in html and "Erwartung" in html
        bad = boss.post("/api/editor/preview", json={"task": {"id": "x", "title": ""}},
                        headers=WRITE)  # fmt: skip
        assert bad.status_code == 422
        assert {e["path"] for e in bad.json()["errors"]} >= {"title", "level"}


# ------------------------------------------------------------------ permissions & CSRF


def test_apprentices_cannot_use_the_editor(config):
    with open_client(config, "anna") as anna:
        assert anna.get("/admin/editor").status_code == 403
        assert anna.get("/admin/editor/demo").status_code == 403
        assert anna.get("/api/editor/demo", headers=READ).status_code == 403
        assert anna.put("/api/editor/demo", json={}, headers=WRITE).status_code == 403
        assert anna.post("/api/editor/preview", json={}, headers=WRITE).status_code == 403


def test_editor_api_csrf(config):
    with open_client(config, "boss") as boss:
        assert boss.get("/api/editor/demo").status_code == 403  # no X-Workbook
        loaded = load(boss)
        body = {"data": loaded["data"], "base_hash": loaded["base_hash"]}
        assert boss.put("/api/editor/demo", json=body, headers=READ).status_code == 403  # no Origin
        assert boss.get("/api/editor/ghost", headers=READ).status_code == 404
        assert boss.get("/admin/editor/..", headers=READ).status_code == 404


def test_editor_pages_render(config):
    with open_client(config, "boss") as boss:
        index = boss.get("/admin/editor").text
        assert 'href="/admin/editor/demo"' in index
        page = boss.get("/admin/editor/demo").text
        assert 'data-editor="demo"' in page and 'src="/static/js/editor.js"' in page


# ------------------------------------------------------------------ unit helpers


def test_clean_drops_empty_optional_values():
    data = {"schema_version": 1, "days": [], "workbook": {"subtitle": "", "signoff": {},
            "header_fields": [], "title": "T"}, "levels": {}}  # fmt: skip
    assert clean(data) == {"schema_version": 1, "days": [], "workbook": {"title": "T"},
                           "levels": {}}  # fmt: skip
    assert clean({"a": "x\r\ny"}) == {"a": "x\ny"}


def test_editor_loc_drops_union_tag():
    assert editor_loc(("days", 0, "blocks", 2, "image", "alt")) == ("days", 0, "blocks", 2, "alt")
    assert editor_loc(("workbook", "text")) == ("workbook", "text")


def test_merge_reorders_by_id_and_keeps_styles():
    yaml = YAML()
    doc = yaml.load("items:\n  - id: a  # first\n    v: 'quoted'\n  - id: b\n    v: 2\n")
    merged = merge(doc, {"items": [{"id": "b", "v": 3}, {"id": "a", "v": "quoted"}, {"id": "c"}]})
    assert [i["id"] for i in merged["items"]] == ["b", "a", "c"]
    assert merged["items"][1]["v"] == "quoted"
    assert prepare("a\nb").endswith("\n")


def test_render_document_for_json_catalogs(tmp_path):
    path = tmp_path / "x.json"
    path.write_text("{}", encoding="utf-8")
    assert json.loads(render_document(path, {"a": "ü"})) == {"a": "ü"}
    assert "ü" in render_document(path, {"a": "ü"})


def test_network_security_roundtrip_is_stable(tmp_path):
    """Saving the converted workbook unchanged must not alter the file."""
    from app.editor import load_for_editor

    path = tmp_path / "ns.yaml"
    original = (ROOT / "workbooks" / "network-security.yaml").read_text(encoding="utf-8")
    path.write_text(original, encoding="utf-8")
    data, _ = load_for_editor(path)
    assert render_document(path, clean(data)) == original
