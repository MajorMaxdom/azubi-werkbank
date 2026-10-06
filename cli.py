"""`workbook` command line interface."""

from __future__ import annotations

from pathlib import Path

import typer

from app.loader import CATALOG_SUFFIXES, load_directory, load_file
from app.renderer import render_static

app = typer.Typer(help="Workbook server tools.", no_args_is_help=True, add_completion=False)

DEFAULT_WORKBOOKS = Path("workbooks")


@app.callback()
def main() -> None:
    """Workbook server tools."""


@app.command()
def validate(
    path: Path = typer.Argument(
        DEFAULT_WORKBOOKS, help="Catalog file or directory (default: workbooks/)."
    ),
) -> None:
    """Validate catalogs. Exit code 1 if any file has errors."""
    if path.is_dir():
        _, results = load_directory(path)
    elif path.is_file():
        results = [load_file(path)]
    else:
        typer.echo(f"Not found: {path}", err=True)
        raise typer.Exit(2)

    failed = 0
    for result in results:
        if result.errors:
            failed += 1
            typer.echo(f"FAIL {result.file}")
            for error in result.errors:
                typer.echo(f"  {error}")
        else:
            assert result.catalog is not None
            meta = result.catalog.workbook
            typer.echo(f"OK   {result.file} ({meta.id} {meta.version})")
    if not results:
        typer.echo(f"No catalog files found in {path}")
    typer.echo(f"{len(results) - failed} valid, {failed} invalid")
    if failed:
        raise typer.Exit(1)


@app.command()
def render(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Catalog file."),
    output: Path = typer.Option(..., "-o", "--output", help="Output HTML file."),
    trainer: bool = typer.Option(
        True, "--trainer/--no-trainer", help="Include the trainer area (expectations, notes)."
    ),
) -> None:
    """Render a catalog to a self-contained static HTML preview."""
    if file.suffix.lower() not in CATALOG_SUFFIXES:
        typer.echo(f"Unsupported file type: {file}", err=True)
        raise typer.Exit(2)
    result = load_file(file)
    if result.errors or result.catalog is None:
        for error in result.errors:
            typer.echo(f"  {error}", err=True)
        raise typer.Exit(1)
    html = render_static(result.catalog, trainer=trainer, workbooks_dir=file.resolve().parent)
    output.write_text(html, encoding="utf-8")
    typer.echo(f"Wrote {output}")


if __name__ == "__main__":
    app()
