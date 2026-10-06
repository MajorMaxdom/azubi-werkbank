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


@app.command()
def schema(
    output: Path = typer.Option(
        Path("workbook.schema.json"), "-o", "--output", help="Output file for the JSON Schema."
    ),
) -> None:
    """Export the JSON Schema of the catalog format (for editor autocompletion)."""
    from app.models.catalog import catalog_json_schema

    output.write_text(catalog_json_schema(), encoding="utf-8")
    typer.echo(f"Wrote {output}")


user_app = typer.Typer(help="Manage users (users.yaml + credentials).", no_args_is_help=True)
app.add_typer(user_app, name="user")

ConfigOption = typer.Option(
    None, "--config", "-c", help="Config file (default: $WORKBOOK_CONFIG or ./config.yaml)."
)


def _accounts(config: Path | None):
    from app.auth import AccountService, CredentialStore, UserDirectory
    from app.config import load_config

    settings = load_config(config)
    directory = UserDirectory(settings.paths.users)
    directory.load()
    if directory.error:
        typer.echo(f"users.yaml is invalid: {directory.error}", err=True)
        raise typer.Exit(1)
    store = CredentialStore(settings.paths.data / "credentials.json")
    return AccountService(settings, directory, store)


@user_app.command("add")
def user_add(
    username: str = typer.Argument(..., help="Username (a-z, 0-9, '-')."),
    name: str = typer.Option(..., "--name", help="Full name shown in the UI."),
    role: str = typer.Option(..., "--role", help="trainer | apprentice"),
    workbook: list[str] = typer.Option(
        None, "--workbook", help="Allowed workbook id (repeatable; default: all)."
    ),
    config: Path | None = ConfigOption,
) -> None:
    """Add a user to users.yaml and print an invite link."""
    from app.auth import is_valid_username

    if role not in ("trainer", "apprentice"):
        typer.echo("--role must be 'trainer' or 'apprentice'", err=True)
        raise typer.Exit(2)
    if not is_valid_username(username):
        typer.echo(f"Invalid username: {username!r}", err=True)
        raise typer.Exit(2)
    accounts = _accounts(config)
    if accounts.directory.get(username) is not None:
        typer.echo(f"User already exists: {username}", err=True)
        raise typer.Exit(1)
    link = accounts.create_user(username, name, role, list(workbook or []) or None)  # type: ignore[arg-type]
    typer.echo(f"Added {username} ({role}). Invite link (single use):")
    typer.echo(link)


@user_app.command("reset")
def user_reset(
    username: str = typer.Argument(...),
    config: Path | None = ConfigOption,
) -> None:
    """Remove the password, end all sessions and print a new invite link."""
    accounts = _accounts(config)
    if accounts.directory.get(username) is None:
        typer.echo(f"Unknown user: {username}", err=True)
        raise typer.Exit(1)
    typer.echo(accounts.reset_access(username))


@user_app.command("list")
def user_list(config: Path | None = ConfigOption) -> None:
    """List users with role and access status."""
    accounts = _accounts(config)
    users = accounts.directory.all()
    if not users:
        typer.echo("No users.")
        return
    for username, user in sorted(users.items()):
        books = "all" if user.workbooks is None else ",".join(user.workbooks)
        status = accounts.status(username)
        typer.echo(f"{username:<20} {user.role:<10} {status:<15} {books:<25} {user.name}")


@app.command()
def serve(config: Path | None = ConfigOption) -> None:
    """Run the web server (uvicorn) with the configured host and port."""
    import logging

    import uvicorn

    from app.config import load_config
    from app.main import create_app

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    settings = load_config(config)
    web_app = create_app(settings)

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:
            # End open SSE streams first; uvicorn waits for open responses on shutdown.
            web_app.state.broadcaster.close()
            super().handle_exit(sig, frame)

    Server(
        uvicorn.Config(
            web_app,
            host=settings.listen_host,
            port=settings.listen_port,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
            timeout_graceful_shutdown=5,
        )
    ).run()


if __name__ == "__main__":
    app()
