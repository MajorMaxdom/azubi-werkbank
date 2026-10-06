from __future__ import annotations

import asyncio
import re
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.admin import localize_error
from app.api.events import Broadcaster, event_stream
from app.config import Config, Paths, load_config
from app.i18n import get_translator
from app.loader import CatalogError, Registry
from app.main import create_app
from app.models.catalog import Stylesheet
from app.theme import contrast_ratio, contrast_warnings
from tests.conftest import ROOT
from tests.test_loader import VALID_YAML


def make_config(tmp_path: Path) -> Config:
    workbooks = tmp_path / "workbooks"
    workbooks.mkdir(exist_ok=True)
    return Config(
        paths=Paths(
            workbooks=workbooks,
            progress=tmp_path / "progress",
            users=tmp_path / "users.yaml",
            data=tmp_path / "data",
            locales=ROOT / "locales",
        )
    )


@pytest.fixture
def config(tmp_path):
    return make_config(tmp_path)


@pytest.fixture
def client(config):
    with TestClient(create_app(config, watch=False)) as c:
        yield c


def write(config: Config, name: str, text: str) -> Path:
    path = config.paths.workbooks / name
    path.write_text(text, encoding="utf-8")
    return path


def reload(client: TestClient) -> Registry:
    registry: Registry = client.app.state.registry
    registry.apply_changes(config_files(registry))
    return registry


def config_files(registry: Registry) -> set[Path]:
    known = {e.path for e in registry.entries()}
    return known | set(registry.directory.iterdir())


# ------------------------------------------------------------------ pages


def test_index_lists_workbooks(config):
    write(config, "a.yaml", VALID_YAML)
    with TestClient(create_app(config, watch=False)) as client:
        html = client.get("/").text
    assert 'href="/workbooks/demo"' in html
    assert "1 Aufgabe" in html


def test_index_empty(client):
    assert "Es sind noch keine Arbeitshefte vorhanden." in client.get("/").text


def test_workbook_page(config):
    write(config, "a.yaml", VALID_YAML)
    with TestClient(create_app(config, watch=False)) as client:
        response = client.get("/workbooks/demo")
    assert response.status_code == 200
    html = response.text
    assert '<link rel="stylesheet" href="/static/css/tokens.css">' in html
    assert "theme.css" not in html  # no stylesheet defined
    assert 'src="/static/js/events.js"' in html
    assert "data-update-banner hidden" in html


@pytest.mark.parametrize("path", ["/workbooks/nope", "/workbooks/UPPER", "/workbooks/a..b"])
def test_unknown_workbook_404(client, path):
    assert client.get(path).status_code == 404
    assert client.get(path + "/theme.css").status_code == 404


def test_network_security_page_renders_without_trainer_content(tmp_path):
    config = make_config(tmp_path)
    shutil.copy(ROOT / "workbooks" / "network-security.yaml", config.paths.workbooks)
    with TestClient(create_app(config, watch=False)) as client:
        html = client.get("/workbooks/network-security").text
    assert "Wie startet ein Linux-System?" in html
    assert "Bereich Ausbilder" not in html
    assert "Reihenfolge A – C – E – D – B." not in html


def test_static_files_served(client):
    response = client.get("/static/css/tokens.css")
    assert response.status_code == 200
    assert client.get("/static/js/events.js").status_code == 200


def test_assets_served_and_traversal_blocked(config):
    assets = config.paths.workbooks / "assets" / "demo"
    assets.mkdir(parents=True)
    (assets / "a.png").write_bytes(b"\x89PNG\r\n")
    (config.paths.workbooks / "secret.yaml").write_text("x: 1")
    with TestClient(create_app(config, watch=False)) as client:
        assert client.get("/assets/demo/a.png").status_code == 200
        assert client.get("/assets/../secret.yaml").status_code == 404
        assert client.get("/assets/%2e%2e/secret.yaml").status_code == 404


def test_missing_assets_dir_is_404(client):
    assert client.get("/assets/x.png").status_code == 404


# ------------------------------------------------------------------ theme.css


def test_theme_css_empty_without_stylesheet(config):
    write(config, "a.yaml", VALID_YAML)
    with TestClient(create_app(config, watch=False)) as client:
        response = client.get("/workbooks/demo/theme.css")
    assert response.status_code == 200
    assert response.text == ""
    assert response.headers["content-type"].startswith("text/css")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["etag"].startswith('"')


def test_theme_css_with_stylesheet_and_etag(config):
    text = VALID_YAML.replace(
        "  title: Demo\n", '  title: Demo\n  stylesheet:\n    accent: "#0F4C3A"\n'
    )
    write(config, "a.yaml", text)
    with TestClient(create_app(config, watch=False)) as client:
        response = client.get("/workbooks/demo/theme.css")
        assert response.text == ":root {\n  --accent: #0F4C3A;\n}\n"
        etag = response.headers["etag"]
        page = client.get("/workbooks/demo").text
        assert f'href="/workbooks/demo/theme.css?v={etag.strip(chr(34))}"' in page
        # tokens.css < theme.css < app.css
        assert page.index("tokens.css") < page.index("theme.css") < page.index("app.css")

        cached = client.get("/workbooks/demo/theme.css", headers={"If-None-Match": etag})
        assert cached.status_code == 304
        assert cached.text == ""
        stale = client.get("/workbooks/demo/theme.css", headers={"If-None-Match": '"old"'})
        assert stale.status_code == 200


def test_theme_etag_changes_after_reload(config):
    path = write(config, "a.yaml", VALID_YAML)
    with TestClient(create_app(config, watch=False)) as client:
        first = client.get("/workbooks/demo/theme.css").headers["etag"]
        path.write_text(
            VALID_YAML.replace("  title: Demo\n", '  title: Demo\n  stylesheet:\n    ink: "#000"\n')
        )
        reload(client)
        second = client.get("/workbooks/demo/theme.css")
    assert second.headers["etag"] != first
    assert "--ink: #000;" in second.text


# ------------------------------------------------------------------ registry / hot reload


def test_registry_add_change_break_delete(config):
    registry = Registry(config.paths.workbooks)
    registry.load_all()
    assert registry.catalogs() == {}

    path = write(config, "a.yaml", VALID_YAML)
    assert registry.apply_changes({path}) == {"demo"}
    assert registry.get("demo").workbook.title == "Demo"

    path.write_text(VALID_YAML.replace("title: Demo", "title: Neu"))
    assert registry.apply_changes({path}) == {"demo"}
    assert registry.get("demo").workbook.title == "Neu"

    # Broken file keeps the last valid version and records the error.
    path.write_text(VALID_YAML.replace("title: Demo", "title: Kaputt\n  unknown: 1"))
    assert registry.apply_changes({path}) == set()
    assert registry.get("demo").workbook.title == "Neu"
    (entry,) = registry.entries()
    assert entry.errors and entry.errors[0].type == "extra_forbidden"

    # Fixing the file clears the error.
    path.write_text(VALID_YAML)
    registry.apply_changes({path})
    assert registry.entries()[0].errors == []

    path.unlink()
    assert registry.apply_changes({path}) == {"demo"}
    assert registry.get("demo") is None
    assert registry.entries() == []


def test_registry_broken_from_start_has_no_catalog(config):
    path = write(config, "a.yaml", "schema_version: 1\n")
    registry = Registry(config.paths.workbooks)
    registry.load_all()
    (entry,) = registry.entries()
    assert entry.catalog is None and entry.errors
    path.write_text(VALID_YAML)
    registry.apply_changes({path})
    assert registry.get("demo") is not None


def test_registry_ignores_underscore_and_other_dirs(config, tmp_path):
    registry = Registry(config.paths.workbooks)
    registry.load_all()
    hidden = write(config, "_draft.yaml", VALID_YAML)
    other = tmp_path / "elsewhere.yaml"
    other.write_text(VALID_YAML)
    assert registry.apply_changes({hidden, other}) == set()
    assert registry.entries() == []


def test_registry_duplicate_ids_first_file_wins(config):
    write(config, "b.yaml", VALID_YAML.replace("title: Demo", "title: B"))
    registry = Registry(config.paths.workbooks)
    registry.load_all()
    assert registry.get("demo").workbook.title == "B"

    a = write(config, "a.yaml", VALID_YAML.replace("title: Demo", "title: A"))
    registry.apply_changes({a})
    assert registry.get("demo").workbook.title == "A"
    b_entry = next(e for e in registry.entries() if e.path.name == "b.yaml")
    assert b_entry.conflict is not None and b_entry.conflict.type == "duplicate_workbook"

    a.unlink()
    registry.apply_changes({a})
    assert registry.get("demo").workbook.title == "B"
    assert all(e.conflict is None for e in registry.entries())


def test_registry_notifies_listeners(config):
    seen: list[set[str]] = []
    registry = Registry(config.paths.workbooks)
    registry.on_change(seen.append)
    registry.load_all()
    path = write(config, "a.yaml", VALID_YAML)
    registry.apply_changes({path})
    path.write_text(VALID_YAML + "\n# comment only\n")
    registry.apply_changes({path})
    assert seen == [{"demo"}, {"demo"}]


def test_view_is_cached_until_catalog_changes(config):
    path = write(config, "a.yaml", VALID_YAML)
    registry = Registry(config.paths.workbooks)
    registry.load_all()
    first = registry.view("demo")
    assert registry.view("demo") is first
    path.write_text(VALID_YAML.replace("title: Demo", "title: Neu"))
    registry.apply_changes({path})
    assert registry.view("demo") is not first


def wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_file_watcher_end_to_end(config):
    """Add, change, break and delete a file while the server runs (no restart)."""
    with TestClient(create_app(config, watch=True)) as client:
        assert client.get("/workbooks/demo").status_code == 404
        time.sleep(0.3)  # let the watcher start

        path = write(config, "a.yaml", VALID_YAML)
        assert wait_for(lambda: client.get("/workbooks/demo").status_code == 200)

        path.write_text(VALID_YAML.replace("title: Demo", "title: Geändert"))
        assert wait_for(lambda: "Geändert" in client.get("/workbooks/demo").text)

        path.write_text(VALID_YAML.replace("duration: 45m", "duration: 45min"))
        assert wait_for(
            lambda: (
                "45min" not in client.get("/admin/catalogs").text
                and "tasks[0].duration" in client.get("/admin/catalogs").text
            )
        )
        assert "Geändert" in client.get("/workbooks/demo").text  # last valid version kept

        path.unlink()
        assert wait_for(lambda: client.get("/workbooks/demo").status_code == 404)


# ------------------------------------------------------------------ admin/catalogs


def test_admin_catalogs_shows_errors_in_german(config):
    write(config, "good.yaml", VALID_YAML)
    write(config, "bad.yaml", VALID_YAML.replace("id: demo", "id: Demo_2"))
    with TestClient(create_app(config, watch=False)) as client:
        html = client.get("/admin/catalogs").text
    assert "good.yaml" in html and "bad.yaml" in html
    assert "<code>workbook.id</code>" in html
    assert "Ungültige ID" in html
    assert "Zeile 3" in html


def test_admin_catalogs_stale_status(config):
    path = write(config, "a.yaml", VALID_YAML)
    with TestClient(create_app(config, watch=False)) as client:
        path.write_text(VALID_YAML.replace("level: understand", "level: expert"))
        reload(client)
        html = client.get("/admin/catalogs").text
    assert "Fehler – letzte gültige Version aktiv" in html
    assert "Unbekannte Stufe „expert“" in html


def test_admin_catalogs_contrast_warning(config):
    text = VALID_YAML.replace(
        "  title: Demo\n", '  title: Demo\n  stylesheet:\n    accent: "#DDDDDD"\n'
    )
    write(config, "a.yaml", text)
    with TestClient(create_app(config, watch=False)) as client:
        html = client.get("/admin/catalogs").text
    assert "Warnungen" in html
    assert "white auf --accent" in html
    assert "--accent auf --card" in html


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CatalogError("f", ("days", 0, "id"), "string_pattern_mismatch", "x"), "Ungültige ID"),
        (
            CatalogError("f", ("days", 0, "modules", 0, "tasks", 0, "duration"),
                         "string_pattern_mismatch", "x"),
            "Ungültige Dauer",
        ),
        (CatalogError("f", ("workbook", "version"), "string_pattern_mismatch", "x"),
         "Keine gültige Versionsnummer"),
        (CatalogError("f", ("workbook", "stylesheet", "accent"), "string_pattern_mismatch", "x"),
         "Keine gültige Farbe"),
        (CatalogError("f", ("x",), "missing", "Field required"), "Pflichtfeld fehlt."),
        (CatalogError("f", ("x",), "extra_forbidden", "x"), "Unbekannter Schlüssel"),
        (CatalogError("f", ("x",), "duplicate_id", "x", ctx={"kind": "task", "id": "t1"}),
         "Aufgaben-ID „t1“ ist doppelt vergeben."),
        (CatalogError("f", ("x",), "literal_error", "x", ctx={"expected": "'a' or 'b'"}),
         "Erlaubt: 'a' or 'b'"),
        (CatalogError("f", (), "parse_error", "found unexpected end"), "Syntaxfehler: found"),
        (CatalogError("f", ("x",), "something_new", "Some message"),
         "Ungültiger Wert (Some message)"),
        (CatalogError("f", ("x",), "duplicate_id", "x"), "Ungültiger Wert (x)"),
    ],
)  # fmt: skip
def test_localize_error(error, expected):
    assert expected in localize_error(error, get_translator())


def test_every_custom_error_type_has_a_german_message():
    t = get_translator()
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "app").rglob("*.py"))

    custom = set(re.findall(r'PydanticCustomError\(\s*"([a-z_]+)"', source))
    custom |= set(re.findall(r'type="([a-z_]+)"', source))
    custom |= set(re.findall(r'"(duplicate_workbook|parse_error|read_error)"', source))
    assert {"duplicate_id", "unknown_level", "duplicate_workbook"} <= custom
    for error_type in custom:
        t.lookup(f"errors.{error_type}")


# ------------------------------------------------------------------ contrast


def test_contrast_ratio_known_values():
    assert round(contrast_ratio("#000000", "#FFFFFF"), 1) == 21.0
    assert round(contrast_ratio("#FFF", "#FFF"), 1) == 1.0


def test_default_theme_has_no_contrast_warnings():
    assert contrast_warnings(None) == []


def test_contrast_warnings_use_merged_colors():
    warnings = contrast_warnings(Stylesheet.model_validate({"paper": "#2A3445"}))
    assert [(w.foreground, w.background) for w in warnings] == [("--ink", "--paper")]


# ------------------------------------------------------------------ events


def test_broadcaster_delivers_and_closes():
    async def scenario():
        broadcaster = Broadcaster()
        broadcaster.bind(asyncio.get_running_loop())
        queue = broadcaster.subscribe()
        stream = event_stream(broadcaster, queue, keepalive=0.05)
        assert await anext(stream) == "retry: 5000\n\n"
        assert await anext(stream) == ": keepalive\n\n"
        broadcaster.publish({"b", "a"})
        assert await anext(stream) == 'event: catalog\ndata: {"workbooks": ["a", "b"]}\n\n'
        broadcaster.close()
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert broadcaster.subscriber_count == 0

    asyncio.run(scenario())


def test_broadcaster_publish_from_thread():
    async def scenario():
        broadcaster = Broadcaster()
        broadcaster.bind(asyncio.get_running_loop())
        queue = broadcaster.subscribe()
        await asyncio.to_thread(broadcaster.publish, {"demo"})
        return await asyncio.wait_for(queue.get(), timeout=2)

    assert asyncio.run(scenario()) == '{"workbooks": ["demo"]}'


# ------------------------------------------------------------------ config


def test_config_defaults_and_file(tmp_path, monkeypatch):
    monkeypatch.delenv("WORKBOOK_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    default = load_config()
    assert default.listen_host == "127.0.0.1"
    assert default.paths.workbooks == tmp_path / "workbooks"

    cfg = tmp_path / "conf" / "config.yaml"
    cfg.parent.mkdir()
    cfg.write_text("listen_port: 9000\npaths:\n  workbooks: books\n")
    loaded = load_config(cfg)
    assert loaded.listen_port == 9000
    assert loaded.paths.workbooks == cfg.parent / "books"

    cfg.write_text("listen_prot: 9000\n")
    with pytest.raises(ValueError):
        load_config(cfg)
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "missing.yaml")
