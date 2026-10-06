from __future__ import annotations

import stat

import pytest

from app.editor import (
    MAX_ASSET_BYTES,
    AssetTooLarge,
    asset_basename,
    detect_image_type,
    store_asset,
)
from tests.conftest import BASE_URL, add_user, make_config, open_client
from tests.test_progress import CATALOG

READ = {"X-Workbook": "1"}
WRITE = {"Origin": BASE_URL, "X-Workbook": "1"}
URL = "/api/editor/demo/assets"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 32
GIF = b"GIF89a\x01\x00\x01\x00" + b"\x00" * 32
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
HTML = b"<!doctype html><html><body><script>alert(1)</script></body></html>"


@pytest.fixture
def config(tmp_path):
    config = make_config(tmp_path)
    (config.paths.workbooks / "demo.yaml").write_text(CATALOG, encoding="utf-8")
    add_user(config, "boss", "trainer", name="Chefin Boss")
    add_user(config, "anna", "apprentice", name="Anna Azubi")
    return config


def upload(client, name, data, content_type="image/png", headers=WRITE, url=URL):
    return client.post(url, files={"file": (name, data, content_type)}, headers=headers)


def asset_dir(config):
    return config.paths.workbooks / "assets" / "demo"


# ------------------------------------------------------------------ upload


@pytest.mark.parametrize(
    ("name", "data", "expected"),
    [
        ("plan.png", PNG, "plan.png"),
        ("Foto.JPEG", JPEG, "foto.jpg"),
        ("anim.gif", GIF, "anim.gif"),
        ("bild.webp", WEBP, "bild.webp"),
    ],
)
def test_upload_allowed_types(config, name, data, expected):
    with open_client(config, "boss") as boss:
        response = upload(boss, name, data)
    assert response.status_code == 200, response.text
    assert response.json()["src"] == f"assets/demo/{expected}"
    path = asset_dir(config) / expected
    assert path.read_bytes() == data
    assert stat.S_IMODE(path.stat().st_mode) == 0o664
    assert not [p for p in asset_dir(config).iterdir() if p.name.startswith(".")]  # no temp left


def test_extension_follows_detected_type(config):
    with open_client(config, "boss") as boss:
        response = upload(boss, "eigentlich.gif", PNG, "image/gif")
    assert response.json()["src"] == "assets/demo/eigentlich.png"


@pytest.mark.parametrize(
    ("name", "data", "content_type"),
    [
        ("logo.svg", SVG, "image/svg+xml"),
        ("seite.html", HTML, "text/html"),
        ("fake.png", HTML, "image/png"),  # fake extension and content type
        ("fake.png", SVG, "image/png"),
        ("leer.png", b"", "image/png"),
        ("halb.webp", b"RIFF\x00\x00\x00\x00WAVE", "image/webp"),
    ],
)
def test_upload_rejects_other_types(config, name, data, content_type):
    with open_client(config, "boss") as boss:
        response = upload(boss, name, data, content_type)
    assert response.status_code in (415, 422)
    assert not asset_dir(config).exists() or not list(asset_dir(config).iterdir())


def test_upload_too_large(config):
    data = PNG + b"\x00" * MAX_ASSET_BYTES
    with open_client(config, "boss") as boss:
        response = upload(boss, "riesig.png", data)
    assert response.status_code == 413
    assert not asset_dir(config).exists() or not list(asset_dir(config).iterdir())


def test_upload_too_large_without_content_length(config):
    """Chunked request bodies are cut off while streaming."""
    boundary = "xyzBOUNDARYxyz"
    head = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode()

    def body():
        yield head + PNG
        for _ in range(6):
            yield b"\x00" * (1024 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    headers = {**WRITE, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    with open_client(config, "boss") as boss:
        response = boss.post(URL, content=body(), headers=headers)
    assert response.status_code == 413


def test_upload_exactly_at_limit_is_ok(config):
    data = PNG + b"\x00" * (MAX_ASSET_BYTES - len(PNG))
    with open_client(config, "boss") as boss:
        response = upload(boss, "genau.png", data)
    assert response.status_code == 200, response.text


def test_upload_without_file_field(config):
    with open_client(config, "boss") as boss:
        response = boss.post(URL, data={"other": "x"}, headers=WRITE)
        assert response.status_code == 422
        response = boss.post(URL, files={"bild": ("a.png", PNG, "image/png")}, headers=WRITE)
        assert response.status_code == 422


def test_filename_sanitized_and_never_overwritten(config):
    with open_client(config, "boss") as boss:
        first = upload(boss, "Größe Übersicht (final).PNG", PNG).json()["src"]
        second = upload(boss, "größe übersicht final.png", PNG + b"2").json()["src"]
        third = upload(boss, "Größe-Übersicht-Final.png", PNG + b"3").json()["src"]
    assert first == "assets/demo/groesse-uebersicht-final.png"
    assert second == "assets/demo/groesse-uebersicht-final-2.png"
    assert third == "assets/demo/groesse-uebersicht-final-3.png"
    assert (asset_dir(config) / "groesse-uebersicht-final.png").read_bytes() == PNG


@pytest.mark.parametrize(
    "name",
    ["../../demo.yaml", "..\\..\\x.png", "/etc/passwd", "../../../outside.png", "....png", ".."],
)
def test_path_traversal_in_names_is_impossible(config, name):
    with open_client(config, "boss") as boss:
        response = upload(boss, name, PNG)
    assert response.status_code == 200, response.text
    src = response.json()["src"]
    assert src.startswith("assets/demo/")
    rest = src.removeprefix("assets/demo/")
    assert "/" not in rest and ".." not in rest
    assert (config.paths.workbooks / "demo.yaml").read_text(encoding="utf-8") == CATALOG
    assert sorted(p.name for p in config.paths.workbooks.iterdir()) == ["assets", "demo.yaml"]


def test_unknown_or_invalid_workbook(config):
    with open_client(config, "boss") as boss:
        assert upload(boss, "a.png", PNG, url="/api/editor/nope/assets").status_code == 404
        assert upload(boss, "a.png", PNG, url="/api/editor/..%2F..%2Fx/assets").status_code == 404
        assert boss.get("/api/editor/nope/assets", headers=READ).status_code == 404
    assert not (config.paths.workbooks / "assets").exists()


# ------------------------------------------------------------------ access control


def test_apprentice_forbidden(config):
    with open_client(config, "anna") as anna:
        assert upload(anna, "a.png", PNG).status_code == 403
        assert anna.get(URL, headers=READ).status_code == 403
        assert anna.post(f"{URL}/a.png/delete", headers=WRITE).status_code == 403
    assert not (config.paths.workbooks / "assets").exists()


def test_anonymous_rejected(config):
    with open_client(config) as client:
        assert upload(client, "a.png", PNG).status_code == 401
    assert not (config.paths.workbooks / "assets").exists()


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": BASE_URL},
        {"X-Workbook": "1"},
        {"Origin": "https://evil.example", "X-Workbook": "1"},
    ],
)
def test_csrf_headers_required(config, headers):
    with open_client(config, "boss") as boss:
        assert upload(boss, "a.png", PNG, headers=headers).status_code == 403
        assert boss.get(URL).status_code == 403  # list needs X-Workbook too
    assert not (config.paths.workbooks / "assets").exists()


# ------------------------------------------------------------------ list, serve, use


def test_list_assets(config):
    other = config.paths.workbooks / "assets" / "other"
    other.mkdir(parents=True)
    (other / "fremd.png").write_bytes(PNG)
    with open_client(config, "boss") as boss:
        assert boss.get(URL, headers=READ).json() == []
        upload(boss, "b.png", PNG)
        upload(boss, "a.gif", GIF)
        (asset_dir(config) / "notiz.txt").write_text("x")
        listed = boss.get(URL, headers=READ).json()
    assert listed == [
        {"src": "assets/demo/a.gif", "name": "a.gif", "size": len(GIF)},
        {"src": "assets/demo/b.png", "name": "b.png", "size": len(PNG)},
    ]


def test_uploaded_image_served_and_usable_in_catalog(config):
    with open_client(config, "boss") as boss:
        src = upload(boss, "Topologie.png", PNG).json()["src"]
        loaded = boss.get("/api/editor/demo", headers=READ).json()
        task = loaded["data"]["days"][0]["modules"][0]["tasks"][1]
        task["blocks"] = [{"type": "image", "src": src, "alt": "Netzplan", "caption": "Abb. 1"}]
        body = {"data": loaded["data"], "base_hash": loaded["base_hash"], "confirm_delete": {}}
        saved = boss.post("/api/editor/demo", json=body, headers=WRITE)
        assert saved.status_code == 200, saved.text
        page = boss.get("/workbooks/demo").text
        assert f'src="/{src}"' in page
        assert 'alt="Netzplan"' in page
    with open_client(config, "anna") as anna:
        served = anna.get("/" + src)
        assert served.status_code == 200
        assert served.content == PNG
        assert served.headers["content-type"] == "image/png"
    with open_client(config) as anonymous:
        assert anonymous.get("/" + src, follow_redirects=False).status_code in (303, 401)


def test_delete_unused_asset_only(config):
    with open_client(config, "boss") as boss:
        used = upload(boss, "used.png", PNG).json()["src"]
        upload(boss, "unused.png", PNG)
        loaded = boss.get("/api/editor/demo", headers=READ).json()
        task = loaded["data"]["days"][0]["modules"][0]["tasks"][1]
        task["blocks"] = [{"type": "image", "src": used, "alt": "Bild"}]
        body = {"data": loaded["data"], "base_hash": loaded["base_hash"], "confirm_delete": {}}
        assert boss.post("/api/editor/demo", json=body, headers=WRITE).status_code == 200

        assert boss.post(f"{URL}/used.png/delete", headers=WRITE).status_code == 409
        assert boss.post(f"{URL}/unused.png/delete", headers=READ).status_code == 403
        assert boss.post(f"{URL}/unused.png/delete", headers=WRITE).status_code == 200
        assert boss.post(f"{URL}/unused.png/delete", headers=WRITE).status_code == 404
        assert boss.post(f"{URL}/..%2F..%2Fdemo.yaml/delete", headers=WRITE).status_code == 404
        names = [a["name"] for a in boss.get(URL, headers=READ).json()]
    assert names == ["used.png"]
    assert (config.paths.workbooks / "demo.yaml").exists()


def test_strings_include_asset_section(config):
    with open_client(config, "boss") as boss:
        strings = boss.get("/api/editor/strings", headers=READ).json()
    assert strings["asset_upload"] == "Hochladen"
    assert strings["add_task"] == "Aufgabe hinzufügen"  # editor_ui still there


# ------------------------------------------------------------------ helpers


def test_detect_image_type():
    assert detect_image_type(PNG) == "png"
    assert detect_image_type(JPEG) == "jpg"
    assert detect_image_type(GIF) == "gif"
    assert detect_image_type(WEBP) == "webp"
    assert detect_image_type(SVG) is None
    assert detect_image_type(b"GIF88a") is None
    assert detect_image_type(b"") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Straße Ähnlich.png", "strasse-aehnlich"),
        ("C:\\Users\\x\\Bild 1.JPG", "bild-1"),
        ("../../etc/passwd", "passwd"),
        (".hidden", "hidden"),
        ("", "bild"),
        (None, "bild"),
        ("***.png", "bild"),
        ("café-crème.webp", "cafe-creme"),
        ("a" * 200 + ".png", "a" * 60),
    ],
)
def test_asset_basename(raw, expected):
    assert asset_basename(raw) == expected


def test_store_asset_stops_streaming_when_too_large(tmp_path):
    consumed = []

    def chunks():
        yield PNG
        for i in range(10):
            consumed.append(i)
            yield b"\x00" * (1024 * 1024)

    with pytest.raises(AssetTooLarge):
        store_asset(tmp_path / "assets" / "demo", "x.png", chunks())
    assert len(consumed) <= 6
    assert list((tmp_path / "assets" / "demo").iterdir()) == []


# ------------------------------------------------------------------ housekeeping page


def _use_image_in_block(config, src: str) -> None:
    path = config.paths.workbooks / "demo.yaml"
    text = path.read_text(encoding="utf-8").replace(
        "            requirement: Tu etwas.\n",
        "            blocks:\n              - {type: image, src: " + src + ", alt: Bild}\n",
    )
    path.write_text(text, encoding="utf-8")


def test_housekeeping_page_lists_usage_and_deletes_unused(config):
    from tests.conftest import ORIGIN, csrf_from

    with open_client(config, "boss") as boss:
        upload(boss, "used.png", PNG)
        upload(boss, "unused.png", PNG)
        upload(boss, "in-text.png", PNG)
        _use_image_in_block(config, "assets/demo/used.png")
        path = config.paths.workbooks / "demo.yaml"
        marker = "          - id: t2\n            title: Aufgabe zwei\n"
        hint = '            hints: "Siehe ![Bild](assets/demo/in-text.png)"\n'
        path.write_text(path.read_text().replace(marker, marker + hint), encoding="utf-8")
        boss.app.state.registry.apply_changes({path})

        page = boss.get("/admin/editor/demo/assets").text
        assert "3 Bilder, davon 1 ungenutzt" in page
        assert "Demo · 1.1 Aufgabe eins" in page  # image block
        assert "Demo · 1.2 Aufgabe zwei" in page  # mentioned in Markdown text
        assert page.count('/assets/unused.png/delete"') == 1
        assert '/assets/used.png/delete"' not in page

        token = csrf_from(page)
        # The used image cannot be deleted, even by a crafted request.
        r = boss.post("/admin/editor/demo/assets/used.png/delete", data={"csrf_token": token},
                      headers=ORIGIN, follow_redirects=False)  # fmt: skip
        assert r.headers["location"].endswith("done=in_use")
        assert (asset_dir(config) / "used.png").exists()
        # Cleanup removes only unused images.
        r = boss.post("/admin/editor/demo/assets/cleanup", data={"csrf_token": token},
                      headers=ORIGIN, follow_redirects=False)  # fmt: skip
        assert r.status_code == 303
        names = sorted(p.name for p in asset_dir(config).iterdir())
        assert names == ["in-text.png", "used.png"]
        assert "Alle ungenutzten Bilder wurden gelöscht." in boss.get(r.headers["location"]).text
        # Without CSRF token or Origin nothing happens.
        upload(boss, "late.png", PNG)
        assert boss.post("/admin/editor/demo/assets/cleanup", headers=ORIGIN).status_code == 403
        assert (asset_dir(config) / "late.png").exists()


def test_housekeeping_page_empty_and_permissions(config):
    with open_client(config, "boss") as boss:
        assert "noch keine Bilder" in boss.get("/admin/editor/demo/assets").text
        assert boss.get("/admin/editor/ghost/assets").status_code == 404
        assert 'href="/admin/editor/demo/assets"' in boss.get("/admin/editor/demo").text
    with open_client(config, "anna") as anna:
        assert anna.get("/admin/editor/demo/assets").status_code == 403
