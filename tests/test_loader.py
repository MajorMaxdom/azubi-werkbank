from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from app.loader import is_catalog_file, load_directory, load_file
from cli import app
from tests.conftest import ROOT

VALID_YAML = """\
schema_version: 1
workbook:
  id: demo
  version: 1.0.0
  title: Demo
levels:
  understand: {label: Verstehen, color: blue}
days:
  - id: day-1
    title: Eins
    modules:
      - code: M-01
        title: Modul
        tasks:
          - id: t1
            title: Aufgabe
            level: understand
            duration: 45m
"""


def write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_repository_catalogs_are_valid():
    for name in ("_template.yaml", "network-security.yaml"):
        result = load_file(ROOT / "workbooks" / name)
        assert result.errors == [], [str(e) for e in result.errors]


def test_template_copy_matches_docs():
    docs = (ROOT / "docs" / "workbook-template.yaml").read_text(encoding="utf-8")
    assert (ROOT / "workbooks" / "_template.yaml").read_text(encoding="utf-8") == docs


def test_yaml_and_json_load(tmp_path):
    yaml_result = load_file(write(tmp_path, "a.yaml", VALID_YAML))
    assert yaml_result.ok
    data = {
        "schema_version": 1,
        "workbook": {"id": "demo-json", "version": "1.0.0", "title": "J"},
        "levels": {"u": {"label": "U", "color": "grey"}},
        "days": [],
    }
    assert load_file(write(tmp_path, "b.json", json.dumps(data))).ok


def test_error_reports_key_path_and_line(tmp_path):
    text = VALID_YAML.replace("duration: 45m", "duration: 45min")
    result = load_file(write(tmp_path, "a.yaml", text))
    assert not result.ok
    (error,) = result.errors
    assert error.path == "days[0].modules[0].tasks[0].duration"
    assert error.line == text.splitlines().index("            duration: 45min") + 1
    assert f":{error.line}:" in str(error)


def test_reference_errors_have_lines(tmp_path):
    text = VALID_YAML.replace("level: understand", "level: expert")
    (error,) = load_file(write(tmp_path, "a.yaml", text)).errors
    assert error.type == "unknown_level"
    assert error.line == text.splitlines().index("            level: expert") + 1


def test_yaml_syntax_error(tmp_path):
    result = load_file(write(tmp_path, "a.yaml", "workbook: [unclosed\n"))
    assert result.errors[0].type == "parse_error"
    assert result.errors[0].line is not None


def test_yaml_duplicate_keys_are_errors(tmp_path):
    text = VALID_YAML.replace("  title: Demo\n", "  title: Demo\n  title: Nochmal\n")
    assert load_file(write(tmp_path, "a.yaml", text)).errors[0].type == "parse_error"


def test_json_syntax_error(tmp_path):
    result = load_file(write(tmp_path, "a.json", '{"a": 1,\n}'))
    assert result.errors[0].type == "parse_error"
    assert result.errors[0].line == 2


def test_top_level_must_be_mapping(tmp_path):
    assert load_file(write(tmp_path, "a.yaml", "- 1\n- 2\n")).errors[0].type == "parse_error"


def test_ignored_files():
    assert is_catalog_file(Path("a.yaml"))
    assert is_catalog_file(Path("a.yml"))
    assert is_catalog_file(Path("a.JSON"))
    assert not is_catalog_file(Path("_template.yaml"))
    assert not is_catalog_file(Path("notes.md"))


def test_directory_duplicate_workbook_ids(tmp_path):
    write(tmp_path, "a.yaml", VALID_YAML)
    write(tmp_path, "b.yaml", VALID_YAML)
    write(tmp_path, "_ignored.yaml", "not: valid")
    write(tmp_path, "readme.txt", "x")
    catalogs, results = load_directory(tmp_path)
    assert list(catalogs) == ["demo"]
    assert [r.file.name for r in results] == ["a.yaml", "b.yaml"]
    assert results[1].errors[0].type == "duplicate_workbook"
    assert results[1].catalog is None


# ------------------------------------------------------------------ CLI


def test_cli_validate(tmp_path):
    runner = CliRunner()
    good = write(tmp_path, "a.yaml", VALID_YAML)
    ok = runner.invoke(app, ["validate", str(good)])
    assert ok.exit_code == 0, ok.output
    assert "1 valid, 0 invalid" in ok.output

    write(tmp_path, "b.yaml", VALID_YAML.replace("version: 1.0.0", "version: '1.0'"))
    bad = runner.invoke(app, ["validate", str(tmp_path)])
    assert bad.exit_code == 1
    assert "workbook.version" in bad.output


def test_cli_validate_default_directory():
    result = CliRunner().invoke(app, ["validate", str(ROOT / "workbooks")])
    assert result.exit_code == 0, result.output


def test_cli_render(tmp_path):
    out = tmp_path / "out.html"
    result = CliRunner().invoke(
        app, ["render", str(ROOT / "workbooks" / "_template.yaml"), "-o", str(out)]
    )
    assert result.exit_code == 0, result.output
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "Arbeitsheft: Beispielthema" in html

    no_trainer = tmp_path / "no-trainer.html"
    CliRunner().invoke(
        app,
        [
            "render",
            str(ROOT / "workbooks" / "_template.yaml"),
            "-o",
            str(no_trainer),
            "--no-trainer",
        ],
    )
    assert "Reihenfolge A – C – E – D – B." not in no_trainer.read_text(encoding="utf-8")


def test_cli_render_invalid_file(tmp_path):
    bad = write(tmp_path, "a.yaml", VALID_YAML.replace("level: understand", "level: x"))
    result = CliRunner().invoke(app, ["render", str(bad), "-o", str(tmp_path / "o.html")])
    assert result.exit_code == 1
    assert not (tmp_path / "o.html").exists()


def test_committed_json_schema_is_up_to_date():
    from app.models.catalog import catalog_json_schema

    committed = (ROOT / "docs" / "workbook.schema.json").read_text(encoding="utf-8")
    assert committed == catalog_json_schema(), "run: werkbank schema -o docs/workbook.schema.json"


def test_cli_schema(tmp_path):
    out = tmp_path / "s.json"
    result = CliRunner().invoke(app, ["schema", "-o", str(out)])
    assert result.exit_code == 0, result.output
    schema = json.loads(out.read_text(encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["$defs"]["Task"]["additionalProperties"] is False
    assert "Never change it" in schema["$defs"]["Task"]["properties"]["id"]["description"]


def test_readme_example_catalog_is_valid(tmp_path):
    import re

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    example = re.search(r"```yaml\n(.*?)```", readme, flags=re.S).group(1)
    result = load_file(write(tmp_path, "readme.yaml", example))
    assert result.errors == [], [str(e) for e in result.errors]
