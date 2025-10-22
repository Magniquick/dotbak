"""Command-line interface for dotbak."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import typer
from rich.console import Console
from rich.table import Table

from .config import DEFAULT_CONFIG_FILENAME, load_config
from .manager import DotbakError, DotbakManager
from .models import (
    ApplyResult,
    RestoreResult,
    StatusEntry,
    StatusReport,
    StatusState,
)

app = typer.Typer(help="Metadata-preserving dotfiles backup manager")
console = Console()


def _load_manager(config: Path | None) -> DotbakManager:
    config_obj = load_config(config)
    return DotbakManager(config_obj)


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        console.print("[red]Permission denied.[/red] Re-run the command with elevated privileges (e.g. `sudo`).")
        raise typer.Exit(code=1) from exc
    if isinstance(exc, DotbakError):
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    raise exc


def _format_apply_results(results: Iterable[ApplyResult]) -> None:
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Group")
    table.add_column("Entry")
    table.add_column("Action")

    for result in results:
        table.add_row(
            result.path.group,
            result.path.relative_path.as_posix(),
            result.action.value,
        )

    console.print(table)


def _format_status(report: StatusReport) -> None:
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Group")
    table.add_column("Entry")
    table.add_column("State")
    table.add_column("Details", overflow="fold")

    status_styles = {
        StatusState.IN_SYNC: "green",
        StatusState.NOT_TRACKED: "yellow",
        StatusState.SOURCE_MISMATCH: "red",
        StatusState.MANAGED_MISSING: "red",
        StatusState.CONTENT_DIFFER: "red",
        StatusState.ORPHANED: "yellow",
    }

    for entry in report.entries:
        style = status_styles.get(entry.state, "white")
        table.add_row(
            entry.path.group,
            entry.path.relative_path.as_posix(),
            f"[{style}]{entry.state.value}[/{style}]",
            entry.details or "",
        )

    console.print(table)


def _format_restore_results(results: Iterable[RestoreResult]) -> None:
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Group")
    table.add_column("Entry")
    table.add_column("Action")
    table.add_column("Details", overflow="fold")

    for result in results:
        table.add_row(
            result.path.group,
            result.path.relative_path.as_posix(),
            result.action.value,
            result.details or "",
        )

    console.print(table)


@app.command()
def init(
    config: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME),
        "--config",
        "-c",
        help="Path to write the configuration file",
        dir_okay=False,
        writable=True,
    ),
    managed_root: str = typer.Option(
        "./managed",
        "--managed-root",
        help="Default managed directory to include in the template",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing config if present"),
) -> None:
    """Create a starter dotbak configuration file."""

    config_path = config
    if config_path.exists() and not force:
        console.print(f"[red]Configuration '{config_path}' already exists. Use --force to overwrite.[/red]")
        raise typer.Exit(code=1)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = f"{managed_root.rstrip('/')}/manifest.toml"
    template = f"""# dotbak configuration

[paths]
# logical name = base path
user_config = "~/.config"

[groups.user_config]
entries = [
  "zsh",
  "wezterm.lua",
]

[settings]
managed_root = "{managed_root}"
manifest_path = "{manifest_path}"
"""

    config_path.write_text(template)
    console.print(f"[green]Created '{config_path}'.[/green]")


@app.command()
def apply(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to dotbak.toml"),
    group: list[str] = typer.Option(None, "--group", "-g", help="Limit to specific group(s)"),
) -> None:
    """Backup files into the managed directory and create symlinks."""

    try:
        manager = _load_manager(config)
        results = manager.apply(group or None)
        _format_apply_results(results)
    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


@app.command()
def status(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to dotbak.toml"),
    group: list[str] = typer.Option(None, "--group", "-g", help="Limit to specific group(s)"),
) -> None:
    """Show managed entries and their current state."""

    try:
        manager = _load_manager(config)
        report = manager.status(group or None)
        _format_status(report)
        if any(entry.state is not StatusState.IN_SYNC for entry in report.entries):
            console.print(
                "[yellow]Some entries are out of sync. Run 'dotbak doctor' for a health summary or 'dotbak apply' to resync.[/yellow]"
            )
    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


@app.command()
def restore(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to dotbak.toml"),
    group: list[str] = typer.Option(None, "--group", "-g", help="Limit to specific group(s)"),
    forget: bool = typer.Option(
        False,
        "--forget",
        help="Remove restored entries from the manifest and delete managed copies",
    ),
) -> None:
    """Replace symlinks with real files from the managed copies."""

    try:
        manager = _load_manager(config)
        results = manager.restore(group or None, forget=forget)
        _format_restore_results(results)
    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


@app.command()
def doctor(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to dotbak.toml"),
    group: list[str] = typer.Option(None, "--group", "-g", help="Limit to specific group(s)"),
) -> None:
    """Run health checks and exit with non-zero status if issues are found."""

    try:
        manager = _load_manager(config)
        report = manager.status(group or None)
        _format_status(report)
        has_issues = any(entry.state is not StatusState.IN_SYNC for entry in report.entries)
        if has_issues:
            console.print("[red]Issues detected. Resolve them or run 'dotbak apply' before proceeding.[/red]")
            raise typer.Exit(code=1)
        console.print("[green]All managed entries are healthy.[/green]")
    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


def run() -> None:
    """Entry point used for console_script bindings."""

    app()
