"""Own account (password change, own data export) and GDPR deletion / data
export of users by a Fachbetreuer."""

from __future__ import annotations

import io
import json
import logging
import zipfile

import pytest
from ruamel.yaml import YAML

from app.auth import CredentialStore, hash_password
from tests.conftest import (
    BASE_URL,
    ORIGIN,
    PASSWORD,
    csrf_from,
    login,
    make_config,
    open_client,
    second_client,
)
from tests.test_progress import CATALOG

NEW_PASSWORD = "ganz-neues-passwort-42"
JSON_HEADERS = {"Origin": BASE_URL, "X-Workbook": "1"}

USERS_YAML = """\
# Team list - keep this comment
users:
  boss:
    name: Chefin Boss
    role: trainer
  kschulz:   # second Fachbetreuer
    name: Karl Schulz
    role: trainer
  anna:
    name: Anna Azubi
    role: apprentice
    supervisors:
      demo:
        default: kschulz
        tasks:
          t1: boss
      other:
        default: boss
        tasks:
          t1: kschulz
  ben:
    name: Ben Azubi
    role: apprentice   # trailing comment stays
    supervisors:
      demo:
        default: kschulz
"""


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(CATALOG, encoding="utf-8")
    other = CATALOG.replace("id: demo", "id: other").replace("title: Demo", "title: Anderes")
    (config.paths.workbooks / "other.yaml").write_text(other, encoding="utf-8")
    config.paths.users.write_text(USERS_YAML, encoding="utf-8")
    store = CredentialStore(config.paths.data / "credentials.json")
    for name in ("boss", "kschulz", "anna", "ben"):

        def change(cred):
            cred.password_hash = hash_password(PASSWORD)

        store.update(name, change)
    return config


def credentials(config) -> dict:
    return json.loads((config.paths.data / "credentials.json").read_text())["users"]


def post_form(client, url: str, data: dict, page: str):
    token = csrf_from(client.get(page).text)
    return client.post(url, data={**data, "csrf_token": token}, headers=ORIGIN)


def change_password(client, current=PASSWORD, new=NEW_PASSWORD, repeat=None):
    data = {"current_password": current, "password": new,
            "password_repeat": new if repeat is None else repeat}  # fmt: skip
    return post_form(client, "/account", data, "/account")


def save_answer(client, workbook: str, text: str) -> None:
    r = client.patch(
        f"/api/progress/{workbook}/tasks/t1",
        json={"answers": {"a1": text}, "done": True},
        headers=JSON_HEADERS,
    )
    assert r.is_success, r.text


# ------------------------------------------------------------------ password change


def test_account_page_is_linked_in_user_menu(config):
    with open_client(config, "anna") as client:
        assert 'href="/account"' in client.get("/").text
        page = client.get("/account")
        assert page.status_code == 200
        assert "Mein Konto" in page.text
        assert 'href="/account/data"' in page.text


def test_password_change_success(config, caplog):
    v0 = credentials(config)["anna"]["session_version"]
    with open_client(config, "anna") as client:
        other = second_client(client, "anna")
        assert other.get("/account").status_code == 200
        with caplog.at_level(logging.INFO):
            r = change_password(client)
        assert r.status_code == 200
        assert "Dein Passwort wurde geändert" in r.text
        assert PASSWORD not in caplog.text and NEW_PASSWORD not in caplog.text
        # This session stays, the other one ended.
        assert client.get("/account").status_code == 200
        assert other.get("/account", follow_redirects=False).status_code == 303
        # Old password no longer works, the new one does.
        fresh = second_client(client)
        assert login(fresh, "anna").status_code == 401
        assert login(fresh, "anna", NEW_PASSWORD).status_code == 303
    assert credentials(config)["anna"]["session_version"] == v0 + 1


def test_wrong_current_password(config, caplog):
    v0 = credentials(config)["anna"]["session_version"]
    with open_client(config, "anna") as client:
        with caplog.at_level(logging.WARNING):
            r = change_password(client, current="falsches-passwort-123")
        assert r.status_code == 400
        assert "Das aktuelle Passwort ist nicht korrekt." in r.text
        assert "auth.password_change_failed user=anna" in caplog.text
        assert "falsches-passwort-123" not in caplog.text
        assert client.get("/account").status_code == 200  # still logged in
    cred = credentials(config)["anna"]
    assert cred["failed_attempts"] == 1
    assert cred["session_version"] == v0
    fresh_login_ok(config, "anna", PASSWORD)


def fresh_login_ok(config, username, password):
    with open_client(config) as client:
        assert login(client, username, password).status_code == 303


def test_wrong_current_password_counts_towards_lockout(config):
    with open_client(config, "anna") as client:
        for _ in range(config.login_max_attempts):
            change_password(client, current="falsches-passwort-123")
        assert credentials(config)["anna"]["locked_until"] is not None
        # While locked, even the right current password is refused.
        assert change_password(client).status_code == 400
        assert login(second_client(client), "anna").status_code == 401


@pytest.mark.parametrize(
    ("new", "repeat", "message"),
    [
        ("kurz", "kurz", "mindestens 12 Zeichen"),
        (NEW_PASSWORD, NEW_PASSWORD + "x", "stimmen nicht überein"),
    ],
)
def test_new_password_rules(config, new, repeat, message):
    v0 = credentials(config)["anna"]["session_version"]
    with open_client(config, "anna") as client:
        r = change_password(client, new=new, repeat=repeat)
        assert r.status_code == 400
        assert message in r.text
    assert credentials(config)["anna"]["session_version"] == v0
    fresh_login_ok(config, "anna", PASSWORD)


def test_password_change_requires_csrf_and_origin(config):
    v0 = credentials(config)["anna"]["session_version"]
    data = {"current_password": PASSWORD, "password": NEW_PASSWORD,
            "password_repeat": NEW_PASSWORD}  # fmt: skip
    with open_client(config, "anna") as client:
        token = csrf_from(client.get("/account").text)
        assert client.post("/account", data=data, headers=ORIGIN).status_code == 403
        r = client.post("/account", data={**data, "csrf_token": token})
        assert r.status_code == 403
        r = client.post(
            "/account", data={**data, "csrf_token": token}, headers={"Origin": "https://evil"}
        )
        assert r.status_code == 403
    assert credentials(config)["anna"]["session_version"] == v0


def test_account_requires_login(config):
    with open_client(config) as client:
        assert client.get("/account", follow_redirects=False).status_code == 303
        assert client.get("/account/data", follow_redirects=False).status_code == 303


# ------------------------------------------------------------------ data export


def read_zip(response) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(response.content))


def test_trainer_exports_user_data_as_zip(config):
    with open_client(config, "boss") as boss:
        anna = second_client(boss, "anna")
        save_answer(anna, "demo", "Annas <b>Antwort</b>")
        save_answer(second_client(boss, "ben"), "demo", "BENS-ANTWORT")
        r = boss.get("/admin/users/anna/data")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/zip"
        disposition = r.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="daten_anna_')
        assert disposition.endswith('.zip"')
        archive = read_zip(r)
        assert sorted(archive.namelist()) == ["html/demo.html", "progress/demo.json", "user.json"]
        user = json.loads(archive.read("user.json"))
        assert user["username"] == "anna"
        assert user["name"] == "Anna Azubi"
        assert user["supervisors"]["demo"]["default"] == "kschulz"
        progress = json.loads(archive.read("progress/demo.json"))
        assert progress["tasks"]["t1"]["answers"]["a1"] == "Annas <b>Antwort</b>"
        html = archive.read("html/demo.html").decode()
        assert "Annas &lt;b&gt;Antwort&lt;/b&gt;" in html
        assert "GEHEIM" not in html  # no trainer content
        everything = b"".join(archive.read(n) for n in archive.namelist())
        assert b"argon2" not in everything
        assert b"password_hash" not in everything
        assert b"session_version" not in everything
        assert b"BENS-ANTWORT" not in everything


def test_own_export_contains_only_own_data(config):
    with open_client(config, "anna") as anna:
        save_answer(anna, "demo", "ANNA-DEMO")
        save_answer(anna, "other", "ANNA-OTHER")
        save_answer(second_client(anna, "ben"), "demo", "BENS-ANTWORT")
        r = anna.get("/account/data")
        assert r.status_code == 200
        assert 'filename="daten_anna_' in r.headers["content-disposition"]
        archive = read_zip(r)
        assert sorted(archive.namelist()) == [
            "html/demo.html", "html/other.html", "progress/demo.json",
            "progress/other.json", "user.json",
        ]  # fmt: skip
        everything = b"".join(archive.read(n) for n in archive.namelist())
        assert b"ANNA-DEMO" in everything and b"ANNA-OTHER" in everything
        assert b"BENS-ANTWORT" not in everything
        assert b"argon2" not in everything


def test_trainer_own_export_is_just_the_user_entry(config):
    with open_client(config, "boss") as boss:
        archive = read_zip(boss.get("/account/data"))
        assert archive.namelist() == ["user.json"]
        assert json.loads(archive.read("user.json"))["role"] == "trainer"


# ------------------------------------------------------------------ deletion


def delete(client, username: str, confirm: str):
    return post_form(
        client,
        f"/admin/users/{username}/delete",
        {"confirm": confirm},
        f"/admin/users/{username}/delete",
    )


def test_users_page_has_export_and_delete_buttons(config):
    with open_client(config, "boss") as boss:
        html = boss.get("/admin/users").text
        assert 'href="/admin/users/anna/data"' in html
        assert 'href="/admin/users/anna/delete"' in html
        assert 'href="/admin/users/kschulz/delete"' in html
        assert 'href="/admin/users/boss/delete"' not in html  # not for oneself
        assert 'href="/admin/users/boss/data"' in html


def test_delete_confirmation_page_lists_everything(config):
    with open_client(config, "boss") as boss:
        save_answer(second_client(boss, "anna"), "demo", "x")
        page = boss.get("/admin/users/anna/delete")
        assert page.status_code == 200
        assert "Anna Azubi" in page.text
        assert "Zugangsdaten" in page.text
        assert "1 Fortschrittsdatei" in page.text
        assert "Demo" in page.text and "1 Aufgabe bearbeitet" in page.text
        trainer_page = boss.get("/admin/users/kschulz/delete").text
        assert "Anna Azubi" in trainer_page and "Ben Azubi" in trainer_page
        assert "2 Zuordnungen" in trainer_page  # anna: demo default + other task


def test_wrong_confirmation_deletes_nothing(config):
    with open_client(config, "boss") as boss:
        r = delete(boss, "anna", "ann")
        assert r.status_code == 400
        assert "stimmt nicht überein" in r.text
        assert boss.app.state.users.get("anna") is not None
    assert "anna" in credentials(config)


def test_delete_removes_all_data(config, caplog):
    with open_client(config, "boss") as boss:
        kschulz = second_client(boss, "kschulz")
        anna = second_client(boss, "anna")
        save_answer(anna, "demo", "x")
        save_answer(anna, "other", "y")
        ben = second_client(boss, "ben")
        save_answer(ben, "demo", "z")
        with caplog.at_level(logging.INFO):
            r = delete(boss, "kschulz", "kschulz")
        assert r.status_code == 200
        assert "Karl Schulz (kschulz) wurde mit allen Daten gelöscht." in r.text
        assert "auth.user_deleted user=kschulz by=boss" in caplog.text
        # The session of the deleted user ended immediately.
        assert kschulz.get("/", follow_redirects=False).status_code == 303

        with caplog.at_level(logging.INFO):
            assert delete(boss, "anna", "anna").status_code == 200
        assert anna.get("/", follow_redirects=False).status_code == 303
        assert login(second_client(boss), "anna").status_code == 401
        assert boss.app.state.users.get("anna") is None
        assert boss.get("/admin/users/anna/delete").status_code == 404

    progress = config.paths.progress
    assert not list(progress.glob("*/anna.json*"))
    assert not list(progress.glob("*/kschulz.json*"))
    assert (progress / "demo" / "ben.json").is_file()
    creds = credentials(config)
    assert "anna" not in creds and "kschulz" not in creds and "ben" in creds

    text = config.paths.users.read_text(encoding="utf-8")
    assert "# Team list - keep this comment" in text
    assert "# trailing comment stays" in text
    assert "kschulz" not in text and "anna" not in text
    data = YAML(typ="safe").load(text)
    assert set(data["users"]) == {"boss", "ben"}
    assert "supervisors" not in data["users"]["ben"]  # only kschulz was assigned


def test_delete_keeps_other_supervisors(config):
    with open_client(config, "boss") as boss:
        assert delete(boss, "kschulz", "kschulz").status_code == 200
        anna = boss.app.state.users.get("anna")
        assert anna.supervisors["demo"].default is None
        assert anna.supervisors["demo"].tasks == {"t1": "boss"}
        assert anna.supervisors["other"].default == "boss"
        assert anna.supervisors["other"].tasks == {}


def test_trainer_cannot_delete_themselves(config):
    with open_client(config, "boss") as boss:
        assert boss.get("/admin/users/boss/delete").status_code == 400
        r = post_form(boss, "/admin/users/boss/delete", {"confirm": "boss"}, "/admin/users")
        assert r.status_code == 400
        assert "eigenes Konto nicht löschen" in r.text
        assert boss.app.state.users.get("boss") is not None


def test_delete_requires_csrf(config):
    with open_client(config, "boss") as boss:
        boss.get("/admin/users")
        r = boss.post("/admin/users/anna/delete", data={"confirm": "anna"}, headers=ORIGIN)
        assert r.status_code == 403
        assert boss.app.state.users.get("anna") is not None


def test_unknown_or_invalid_user_is_404(config):
    with open_client(config, "boss") as boss:
        assert boss.get("/admin/users/nobody/delete").status_code == 404
        assert boss.get("/admin/users/nobody/data").status_code == 404
        assert boss.get("/admin/users/Anna/data").status_code == 404


def test_apprentice_gets_403_on_admin_delete_and_export(config):
    with open_client(config, "anna") as anna:
        assert anna.get("/admin/users/ben/data").status_code == 403
        assert anna.get("/admin/users/ben/delete").status_code == 403
        token = csrf_from(anna.get("/account").text)
        r = anna.post(
            "/admin/users/ben/delete",
            data={"confirm": "ben", "csrf_token": token},
            headers=ORIGIN,
        )
        assert r.status_code == 403
        assert anna.app.state.users.get("ben") is not None


def test_recreated_user_does_not_accept_old_session(tmp_path):
    """A cookie of a deleted account must not log into a new account of the same name."""
    from tests.conftest import add_user, make_config, open_client, second_client

    config = make_config(tmp_path)
    add_user(config, "boss", "trainer")
    add_user(config, "anna", "apprentice")
    with open_client(config, "boss") as boss:
        old = second_client(boss, "anna")
        assert old.get("/", follow_redirects=False).status_code == 200
        accounts = boss.app.state.accounts
        accounts.delete_user("anna", boss.app.state.progress, by="boss")
        assert old.get("/", follow_redirects=False).status_code == 303
        link = accounts.create_user("anna", "Anna Neu", "apprentice")
        token = link.rsplit("/", 1)[1]
        assert accounts.accept_invite(token, "neues-langes-passwort") == "anna"
        # The old browser still holds the old cookie: it must stay logged out.
        assert old.get("/", follow_redirects=False).status_code == 303
