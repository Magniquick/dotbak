"""Command-line interface for dotbak."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

import typer
from rich.console import Console
from rich.table import Table

from .config import load_config
from .manager import DotbakError, DotbakManager
from .models import ApplyAction, ApplyResult, StatusEntry, StatusReport, StatusState

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
    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


def run() -> None:
    """Entry point used for console_script bindings."""

    app()
