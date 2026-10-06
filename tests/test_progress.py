from __future__ import annotations

import json
import stat
import threading

import pytest

from tests.conftest import BASE_URL, add_user, make_config, open_client, second_client

CATALOG = """\
schema_version: 1
workbook:
  id: demo
  version: 1.2.0
  title: Demo
  header_fields:
    - {id: start, label: Start, type: date}
    - {id: period, label: Zeitraum, type: short}
levels:
  u: {label: Verstehen, color: blue}
days:
  - id: day-1
    title: Eins
    modules:
      - code: M-01
        title: Modul
        tasks:
          - id: t1
            title: Aufgabe eins
            level: u
            requirement: Tu etwas.
            answers:
              - {id: a1, label: Text}
              - {id: cl, type: checklist, items: [Aussteller, Kette]}
              - {id: ch, type: choice, options: [TCP 80, TCP 443]}
              - {id: chm, type: choice, multiple: true, options: [A, B, C]}
              - {id: dt, type: date}
            trainer:
              expectations: [GEHEIM]
          - id: t2
            title: Aufgabe zwei
            level: u
            answers:
              - {id: a1}
"""

API = "/api/progress/demo"
JSON_HEADERS = {"Origin": BASE_URL, "X-Workbook": "1"}


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(CATALOG, encoding="utf-8")
    add_user(config, "boss", "trainer")
    add_user(config, "anna", "apprentice", name="Anna")
    add_user(config, "ben", "apprentice", name="Ben")
    return config


def patch(client, url, body, headers=JSON_HEADERS):
    return client.patch(url, json=body, headers=headers)


def progress_file(config, user="anna"):
    return config.paths.progress / "demo" / f"{user}.json"


def stored(config, user="anna") -> dict:
    return json.loads(progress_file(config, user).read_text())


# ------------------------------------------------------------------ saving & reloading


def test_save_answers_done_and_header_then_reload(config):
    with open_client(config, "anna") as client:
        r = patch(client, f"{API}/tasks/t1", {"answers": {"a1": "Meine <b>Antwort</b>"}})
        assert r.status_code == 200, r.text
        assert r.json()["progress"] == {"done": 0, "total": 2}
        r = patch(client, f"{API}/tasks/t1", {
            "answers": {"cl": ["Kette", "Aussteller"], "ch": "TCP 443", "chm": ["C", "A"],
                        "dt": "2026-10-06"},
            "done": True,
        })  # fmt: skip
        assert r.json()["progress"] == {"done": 1, "total": 2}
        assert r.json()["task"]["done"] is True
        assert patch(client, f"{API}/header", {"start": "2026-10-05", "period": "KW 41"}).is_success

        html = client.get("/workbooks/demo").text
    assert "Meine &lt;b&gt;Antwort&lt;/b&gt;</textarea>" in html  # escaped user input
    assert 'value="Aussteller" checked' in html and 'value="Kette" checked' in html
    assert 'value="TCP 443" checked' in html and 'value="TCP 80">' in html
    assert 'value="A" checked' in html and 'value="B">' in html
    assert 'type="date" value="2026-10-06"' in html
    assert 'data-header="start" type="date" value="2026-10-05"' in html
    assert 'id="done-t1" data-done checked' in html
    assert 'class="port is-lit" data-port="t1"' in html
    assert 'value="1" aria-label="Gesamtfortschritt"' in html
    assert "1 / 2 erledigt (50 %)" in html
    assert "data-autosave" in html and "data-save-status" in html

    data = stored(config)
    assert data["workbook"] == "demo" and data["user"] == "anna"
    assert data["workbook_version"] == "1.2.0"
    task = data["tasks"]["t1"]
    assert task["answers"]["cl"] == ["Aussteller", "Kette"]  # catalog order
    assert task["answers"]["chm"] == ["A", "C"]
    assert task["done_at"] and task["task_hash"] and task["updated_at"]
    assert data["header"] == {"start": "2026-10-05", "period": "KW 41"}


def test_undone_clears_done_at(config):
    with open_client(config, "anna") as client:
        patch(client, f"{API}/tasks/t2", {"done": True})
        assert stored(config)["tasks"]["t2"]["done_at"]
        patch(client, f"{API}/tasks/t2", {"done": False})
    assert stored(config)["tasks"]["t2"]["done"] is False
    assert stored(config)["tasks"]["t2"]["done_at"] is None


def test_start_page_shows_own_progress(config):
    with open_client(config, "anna") as client:
        patch(client, f"{API}/tasks/t1", {"done": True})
        assert "1 von 2 erledigt" in client.get("/").text


def test_files_are_private(config):
    with open_client(config, "anna") as client:
        patch(client, f"{API}/tasks/t1", {"answers": {"a1": "x"}})
    assert stat.S_IMODE(progress_file(config).stat().st_mode) == 0o600
    assert stat.S_IMODE(progress_file(config).parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(config.paths.progress.stat().st_mode) == 0o700


# ------------------------------------------------------------------ isolation & permissions


def test_two_users_never_see_each_others_data(config):
    with open_client(config, "anna") as anna:
        ben = second_client(anna, "ben")
        patch(anna, f"{API}/tasks/t1", {"answers": {"a1": "ANNA-GEHEIM"}, "done": True})
        patch(ben, f"{API}/tasks/t1", {"answers": {"a1": "BEN-TEXT"}})
        assert "ANNA-GEHEIM" not in ben.get("/workbooks/demo").text
        assert "BEN-TEXT" not in anna.get("/workbooks/demo").text
        assert "0 / 2 erledigt" in ben.get("/workbooks/demo").text
        # There is no way to address another user's progress.
        assert patch(
            ben, "/api/progress/demo/users/anna/tasks/t1", {"answers": {}}
        ).status_code in (
            403,
            404,
            405,
        )
    assert stored(config, "anna")["tasks"]["t1"]["answers"]["a1"] == "ANNA-GEHEIM"
    assert stored(config, "ben")["tasks"]["t1"]["answers"]["a1"] == "BEN-TEXT"


def test_username_in_body_is_ignored(config):
    with open_client(config, "ben") as client:
        r = patch(client, f"{API}/tasks/t1", {"answers": {"a1": "x"}, "user": "anna"})
        assert r.status_code == 422
    assert not progress_file(config, "anna").exists()


def test_trainer_cannot_write_apprentice_fields(config):
    with open_client(config, "boss") as client:
        assert patch(client, f"{API}/tasks/t1", {"answers": {"a1": "x"}}).status_code == 403
        assert patch(client, f"{API}/header", {"period": "x"}).status_code == 403
        html = client.get("/workbooks/demo").text
    assert "data-autosave" not in html
    assert "Vorschau: Als Fachbetreuer" in html
    assert 'id="done-t1" data-done disabled' in html
    assert not config.paths.progress.exists() or not any(config.paths.progress.rglob("*.json"))


@pytest.mark.parametrize(
    "body",
    [
        {"review": {"status": "ok"}},
        {"done_at": "2026-01-01T00:00:00Z"},
        {"task_hash": "x"},
        {"answers": {"a1": "x"}, "signoff": {}},
    ],
)
def test_apprentice_cannot_write_review_or_bookkeeping(config, body):
    with open_client(config, "anna") as client:
        assert patch(client, f"{API}/tasks/t1", body).status_code == 422
    assert not progress_file(config).exists()


def test_apprentice_cannot_write_other_workbooks(config):
    (config.paths.workbooks / "other.yaml").write_text(
        CATALOG.replace("id: demo", "id: other"), encoding="utf-8"
    )
    add_user(config, "carl", "apprentice", workbooks=["demo"])
    with open_client(config, "carl") as client:
        r = patch(client, "/api/progress/other/tasks/t1", {"answers": {"a1": "x"}})
        assert r.status_code == 404
        assert patch(client, "/api/progress/nope/tasks/t1", {"done": True}).status_code == 404
        assert patch(client, "/api/progress/..%2Fx/tasks/t1", {"done": True}).status_code == 404


# ------------------------------------------------------------------ CSRF for the JSON API


def test_json_api_requires_header_origin_and_session(config):
    with open_client(config, "anna") as client:
        body = {"answers": {"a1": "x"}}
        assert (
            patch(client, f"{API}/tasks/t1", body, headers={"Origin": BASE_URL}).status_code == 403
        )
        assert (
            patch(client, f"{API}/tasks/t1", body, headers={"X-Workbook": "1"}).status_code == 403
        )
        evil = {"Origin": "https://evil.example", "X-Workbook": "1"}
        assert patch(client, f"{API}/tasks/t1", body, headers=evil).status_code == 403
        assert client.post(f"{API}/tasks/t1", json=body, headers=JSON_HEADERS).status_code == 405
    with open_client(config) as anonymous:
        assert patch(anonymous, f"{API}/tasks/t1", {"done": True}).status_code == 401


# ------------------------------------------------------------------ validation (422)


@pytest.mark.parametrize(
    ("url", "body"),
    [
        (f"{API}/tasks/nope", {"done": True}),
        (f"{API}/tasks/t1", {"answers": {"nope": "x"}}),
        (f"{API}/tasks/t1", {"answers": {"a1": 5}}),
        (f"{API}/tasks/t1", {"answers": {"cl": "Aussteller"}}),
        (f"{API}/tasks/t1", {"answers": {"cl": ["Unbekannt"]}}),
        (f"{API}/tasks/t1", {"answers": {"ch": "UDP 53"}}),
        (f"{API}/tasks/t1", {"answers": {"ch": ["TCP 80"]}}),
        (f"{API}/tasks/t1", {"answers": {"chm": "A"}}),
        (f"{API}/tasks/t1", {"answers": {"dt": "06.10.2026"}}),
        (f"{API}/tasks/t1", {"done": "yes"}),
        (f"{API}/tasks/t1", ["not", "an", "object"]),
        (f"{API}/header", {"nope": "x"}),
        (f"{API}/header", {"start": "morgen"}),
        (f"{API}/header", {"period": 3}),
        (f"{API}/header", "text"),
    ],
)
def test_invalid_input_is_422(config, url, body):
    with open_client(config, "anna") as client:
        assert patch(client, url, body).status_code == 422
    assert not progress_file(config).exists()


def test_empty_values_are_allowed(config):
    with open_client(config, "anna") as client:
        r = patch(client, f"{API}/tasks/t1", {"answers": {"ch": "", "dt": "", "cl": []}})
        assert r.status_code == 200
        assert patch(client, f"{API}/header", {"start": ""}).status_code == 200


def test_invalid_json_is_422(config):
    with open_client(config, "anna") as client:
        r = client.patch(
            f"{API}/tasks/t1",
            content=b"{not json",
            headers={**JSON_HEADERS, "Content-Type": "application/json"},
        )
        assert r.status_code == 422


# ------------------------------------------------------------------ size limits


def test_answer_size_limit(config):
    with open_client(config, "anna") as client:
        ok = patch(client, f"{API}/tasks/t1", {"answers": {"a1": "x" * 20_000}})
        assert ok.status_code == 200
        too_long = patch(client, f"{API}/tasks/t1", {"answers": {"a1": "x" * 20_001}})
        assert too_long.status_code == 422
        header = patch(client, f"{API}/header", {"period": "x" * 20_001})
        assert header.status_code == 422
    assert len(stored(config)["tasks"]["t1"]["answers"]["a1"]) == 20_000


def test_request_size_limit(config):
    with open_client(config, "anna") as client:
        big = json.dumps({"answers": {"a1": "x" * 1_000_001}}).encode()
        r = client.patch(
            f"{API}/tasks/t1",
            content=big,
            headers={**JSON_HEADERS, "Content-Type": "application/json"},
        )
        assert r.status_code == 413
        lying = client.patch(
            f"{API}/tasks/t1",
            content=big,
            headers={**JSON_HEADERS, "Content-Type": "application/json", "Content-Length": "10"},
        )
        assert lying.status_code in (400, 413, 422)
    assert not progress_file(config).exists()


# ------------------------------------------------------------------ concurrency


def test_concurrent_patches_do_not_corrupt(config):
    errors: list[str] = []
    with open_client(config, "anna") as client:

        def worker(n: int) -> None:
            r = patch(client, f"{API}/tasks/t{1 + n % 2}", {"answers": {"a1": f"value-{n}"}})
            if r.status_code != 200:
                errors.append(r.text)

        def done_worker(n: int) -> None:
            r = patch(client, f"{API}/tasks/t2", {"done": n % 2 == 0})
            if r.status_code != 200:
                errors.append(r.text)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(40)]
        threads += [threading.Thread(target=done_worker, args=(n,)) for n in range(10)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    assert errors == []
    data = stored(config)  # valid JSON
    assert data["tasks"]["t1"]["answers"]["a1"].startswith("value-")
    assert data["tasks"]["t2"]["answers"]["a1"].startswith("value-")
    leftovers = list(progress_file(config).parent.glob(".*.tmp"))
    assert leftovers == []


# ------------------------------------------------------------------ catalog changes


def test_changed_task_hint(config):
    with open_client(config, "anna") as client:
        patch(client, f"{API}/tasks/t1", {"answers": {"a1": "x"}})
        hint = "Diese Aufgabe wurde geändert, nachdem du sie bearbeitet hast."
        assert hint not in client.get("/workbooks/demo").text

        registry = client.app.state.registry
        path = config.paths.workbooks / "demo.yaml"
        # Trainer-only content does not count as a change.
        path.write_text(CATALOG.replace("[GEHEIM]", "[GEHEIM, NEU]"), encoding="utf-8")
        registry.apply_changes({path})
        assert hint not in client.get("/workbooks/demo").text

        path.write_text(CATALOG.replace("Tu etwas.", "Tu etwas anderes."), encoding="utf-8")
        registry.apply_changes({path})
        html = client.get("/workbooks/demo").text
        assert html.count(hint) == 1
        r = patch(client, f"{API}/tasks/t1", {"answers": {"a1": "y"}})
        assert r.json()["task"]["changed"] is False
        assert hint not in client.get("/workbooks/demo").text


def test_orphaned_answers_are_kept(config):
    with open_client(config, "anna") as client:
        patch(client, f"{API}/tasks/t2", {"answers": {"a1": "bleibt"}, "done": True})
        path = config.paths.workbooks / "demo.yaml"
        without_t2 = CATALOG.split("          - id: t2")[0]
        path.write_text(without_t2, encoding="utf-8")
        client.app.state.registry.apply_changes({path})
        assert patch(client, f"{API}/tasks/t2", {"done": False}).status_code == 422
        r = patch(client, f"{API}/tasks/t1", {"done": True})
        assert r.json()["progress"] == {"done": 1, "total": 1}  # orphan not counted
        html = client.get("/workbooks/demo").text
        assert "bleibt" not in html
    assert stored(config)["tasks"]["t2"]["answers"]["a1"] == "bleibt"


def test_corrupt_progress_file_is_not_overwritten(config):
    target = progress_file(config)
    target.parent.mkdir(parents=True)
    target.write_text("{broken", encoding="utf-8")
    with open_client(config, "anna") as client:
        assert patch(client, f"{API}/tasks/t1", {"done": True}).status_code == 500
        page = client.get("/workbooks/demo")
        assert page.status_code == 500
        assert "Es wurde nichts überschrieben" in page.text
    assert target.read_text() == "{broken"
