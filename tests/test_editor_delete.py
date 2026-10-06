from __future__ import annotations

import json

import pytest

from tests.conftest import (
    BASE_URL,
    ORIGIN,
    add_user,
    csrf_from,
    make_config,
    open_client,
    second_client,
)
from tests.test_progress import CATALOG

WRITE = {"Origin": BASE_URL, "X-Workbook": "1"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(CATALOG, encoding="utf-8")
    (config.paths.workbooks / "other.yaml").write_text(
        CATALOG.replace("id: demo", "id: other"), encoding="utf-8"
    )
    add_user(config, "boss", "trainer", name="Chefin Boss")
    add_user(config, "anna", "apprentice", name="Anna Azubi", workbooks=["demo", "other"])
    add_user(config, "ben", "apprentice", name="Ben Azubi", workbooks=["demo"])
    text = config.paths.users.read_text().replace(
        "  ben:\n    name: Ben Azubi\n    role: apprentice\n    workbooks: [demo]\n",
        "  ben:  # Kommentar bleibt\n    name: Ben Azubi\n    role: apprentice\n"
        "    workbooks: [demo]\n    supervisors:\n      demo:\n        default: boss\n",
    )
    config.paths.users.write_text(text, encoding="utf-8")
    return config


def delete(client, workbook="demo", **form):
    token = csrf_from(client.get(f"/admin/editor/{workbook}/delete").text)
    return client.post(f"/admin/editor/{workbook}/delete", data={**form, "csrf_token": token},
                       headers=ORIGIN, follow_redirects=False)  # fmt: skip


def save_progress(boss):
    anna = second_client(boss, "anna")
    r = anna.patch("/api/progress/demo/tasks/t1", json={"answers": {"a1": "x"}}, headers=WRITE)
    assert r.status_code == 200


def test_confirmation_page_shows_impact(config):
    with open_client(config, "boss") as boss:
        save_progress(boss)
        boss.post("/api/editor/demo/assets", files={"file": ("bild.png", PNG, "image/png")},
                  headers=WRITE)  # fmt: skip
        page = boss.get("/admin/editor/demo/delete").text
        assert 'href="/admin/editor/demo/delete"' in boss.get("/admin/editor").text
    assert "Katalogdatei demo.yaml" in page
    assert "1 Azubi hat bereits Antworten" in page
    assert "Zuordnungen werden bei 2 Nutzern entfernt: Anna Azubi, Ben Azubi" in page
    assert "1 Bild gehört zu diesem Heft." in page
    assert 'name="delete_progress"' in page and 'name="delete_assets"' in page


def test_wrong_confirmation_deletes_nothing(config):
    with open_client(config, "boss") as boss:
        response = delete(boss, confirm="falsch")
        assert response.status_code == 400
        assert "stimmt nicht" in response.text
        assert boss.get("/workbooks/demo").status_code == 200
    assert (config.paths.workbooks / "demo.yaml").exists()


def test_delete_keeps_answers_by_default_and_archives_file(config):
    with open_client(config, "boss") as boss:
        save_progress(boss)
        response = delete(boss, confirm="demo")
        assert response.status_code == 303
        assert response.headers["location"] == "/admin/editor?deleted=demo"
        assert "Das Heft „demo“ wurde gelöscht." in boss.get(response.headers["location"]).text
        assert boss.get("/workbooks/demo").status_code == 404
        assert boss.get("/workbooks/other").status_code == 200
        anna = second_client(boss, "anna")
        assert 'href="/workbooks/demo"' not in anna.get("/").text
    assert not (config.paths.workbooks / "demo.yaml").exists()
    archived = list((config.paths.workbooks / "_backups" / "deleted").glob("demo.*.yaml"))
    assert len(archived) == 1 and "title: Demo" in archived[0].read_text()
    # Answers are kept unless explicitly deleted.
    stored = json.loads((config.paths.progress / "demo" / "anna.json").read_text())
    assert stored["tasks"]["t1"]["answers"]["a1"] == "x"
    # users.yaml: references removed, comments kept, an emptied list stays [] (= no workbook).
    users = config.paths.users.read_text()
    assert "  anna:\n    name: Anna Azubi\n    role: apprentice\n    workbooks: [other]\n" in users
    assert "  ben:  # Kommentar bleibt\n" in users
    assert "workbooks: []" in users and "supervisors" not in users


def test_delete_with_answers_and_images(config):
    with open_client(config, "boss") as boss:
        save_progress(boss)
        boss.post("/api/editor/demo/assets", files={"file": ("bild.png", PNG, "image/png")},
                  headers=WRITE)  # fmt: skip
        response = delete(boss, confirm="demo", delete_progress="1", delete_assets="1")
        assert response.status_code == 303
        ben = second_client(boss, "ben")
        assert "Es sind noch keine Arbeitshefte vorhanden." in ben.get("/").text  # [] = none
    assert not (config.paths.progress / "demo").exists()
    assert not (config.paths.workbooks / "assets" / "demo").exists()


def test_restore_by_moving_the_file_back(config):
    with open_client(config, "boss") as boss:
        save_progress(boss)
        delete(boss, confirm="demo")
        archived = next((config.paths.workbooks / "_backups" / "deleted").glob("demo.*.yaml"))
        target = config.paths.workbooks / "demo.yaml"
        archived.rename(target)
        boss.app.state.registry.apply_changes({target})
        assert boss.get("/workbooks/demo").status_code == 200
        # Assignments were removed on delete and have to be set again.
        assert boss.get("/workbooks/demo/users/anna").status_code == 404
    stored = json.loads((config.paths.progress / "demo" / "anna.json").read_text())
    assert stored["tasks"]["t1"]["answers"]["a1"] == "x"  # answers are back with the file


def test_delete_permissions_and_csrf(config):
    with open_client(config, "anna") as anna:
        assert anna.get("/admin/editor/demo/delete").status_code == 403
        assert anna.post("/admin/editor/demo/delete", data={"confirm": "demo"},
                         headers=ORIGIN).status_code == 403  # fmt: skip
    with open_client(config, "boss") as boss:
        assert boss.post("/admin/editor/demo/delete", data={"confirm": "demo"},
                         headers=ORIGIN).status_code == 403  # no CSRF token  # fmt: skip
        assert boss.get("/admin/editor/ghost/delete").status_code == 404
    assert (config.paths.workbooks / "demo.yaml").exists()
