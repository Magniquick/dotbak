"""Command-line interface for dotbak."""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import tomli_w
import typer
from rich.console import Console
from rich.table import Table

from .config import DEFAULT_CONFIG_FILENAME, ConfigError, load_config
from .manager import DotbakError, DotbakManager
from .models import ApplyResult, ManagedPath, RestoreResult, StatusEntry, StatusReport, StatusState

app = typer.Typer(help="Metadata-preserving dotfiles backup manager")
console = Console()


def _load_manager(config: Path | None) -> DotbakManager:
    config_obj = load_config(config)
    return DotbakManager(config_obj)


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, PermissionError):
        console.print("[red]Permission denied.[/red] Re-run the command with elevated privileges (e.g. `sudo`).")
        raise typer.Exit(code=1)
    if isinstance(exc, ConfigError):
        message = str(exc)
        console.print(f"[red]{message}[/red]")
        if "does not exist" in message:
            console.print("[yellow]Use 'dotbak init --config <path>' to create a configuration file.[/yellow]")
        elif "Expected to find" in message:
            console.print(
                "[yellow]Make sure you pointed to the directory containing the config file, or to the file itself.[/yellow]"
            )
        raise typer.Exit(code=1)
    if isinstance(exc, DotbakError):
        console.print(f"[red]{exc}[/red]")
        lowered = str(exc).lower()
        if "insufficient permissions" in lowered or "elevated privileges" in lowered:
            console.print(
                "[yellow]Tip: try rerunning with `sudo` or grant write access to the target directories.[/yellow]"
            )
        raise typer.Exit(code=1)
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


def _format_permission_issues(issues: Iterable[tuple[ManagedPath, str]]) -> None:
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Group")
    table.add_column("Entry")
    table.add_column("Reason", overflow="fold")

    for managed_path, reason in issues:
        table.add_row(managed_path.group, managed_path.relative_path.as_posix(), reason)

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


@dataclass
class DiscoveredGroup:
    name: str
    raw_path: str
    resolved_path: Path
    entries: list[str]


def _parse_discovery_arg(raw: str) -> tuple[str, str]:
    if "=" in raw:
        name, path = raw.split("=", 1)
        return name.strip(), path.strip()
    return "", raw.strip()


def _sanitize_group_name(value: str) -> str:
    sanitized = re.sub(r"[^0-9A-Za-z]+", "_", value.strip().lower())
    sanitized = sanitized.strip("_")
    return sanitized or "group"


def _resolve_discovery_path(raw_path: str, config_dir: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        candidate = (config_dir / candidate).resolve()
    return candidate


def _discover_entries(base_path: Path) -> list[str]:
    try:
        children = list(base_path.iterdir())
    except FileNotFoundError:
        return []
    entries = [child.relative_to(base_path).as_posix() for child in children]
    return sorted(entries)


def _build_discovery(config_dir: Path, raw_discover: list[str] | None) -> list[DiscoveredGroup]:
    if not raw_discover:
        return []

    groups: list[DiscoveredGroup] = []
    seen: dict[str, int] = {}

    for raw in raw_discover:
        name_candidate, path_str = _parse_discovery_arg(raw)
        resolved = _resolve_discovery_path(path_str, config_dir)

        if not name_candidate:
            derived_name = resolved.name if resolved.name else (resolved.parts[-1] if resolved.parts else "group")
            base_name = _sanitize_group_name(derived_name)
        else:
            base_name = _sanitize_group_name(name_candidate)

        count = seen.get(base_name, 0)
        group_name = base_name if count == 0 else f"{base_name}_{count + 1}"
        seen[base_name] = count + 1

        if not resolved.exists():
            console.print(
                f"[yellow]Discovery path '{path_str}' does not exist; generating empty group '{group_name}'.[/yellow]"
            )
            entries: list[str] = []
        else:
            entries = _discover_entries(resolved)

        groups.append(
            DiscoveredGroup(
                name=group_name,
                raw_path=path_str,
                resolved_path=resolved,
                entries=entries,
            )
        )

    return groups


def _render_init_config(*, managed_root: str, manifest_path: str, discovered: list[DiscoveredGroup]) -> str:
    if not discovered:
        return f"""# dotbak configuration

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

    data = {
        "paths": {group.name: group.raw_path for group in discovered},
        "groups": {group.name: {"entries": group.entries} for group in discovered},
        "settings": {
            "managed_root": managed_root,
            "manifest_path": manifest_path,
        },
    }

    buffer = io.StringIO()
    buffer.write("# dotbak configuration\n\n")
    buffer.write(tomli_w.dumps(data))
    return buffer.getvalue()


def _bootstrap_managed_dirs(config_dir: Path, managed_root: str, discovered: list[DiscoveredGroup]) -> None:
    managed_root_path = Path(managed_root).expanduser()
    if not managed_root_path.is_absolute():
        managed_root_path = (config_dir / managed_root_path).resolve()

    managed_root_path.mkdir(parents=True, exist_ok=True)
    console.print(f"[green]Ensured managed root '{managed_root_path}'.[/green]")

    for group in discovered:
        group_dir = managed_root_path / group.name
        group_dir.mkdir(parents=True, exist_ok=True)
        console.print(f"[green]Ensured managed directory '{group_dir}'.[/green]")


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
    discover: list[str] = typer.Option(
        None,
        "--discover",
        help="Discover entries from GROUP=PATH (or just PATH to auto-name group)",
    ),
    bootstrap_managed: bool = typer.Option(
        False,
        "--bootstrap-managed/--no-bootstrap-managed",
        help="Create the managed directory structure after writing the config",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing config if present"),
) -> None:
    """Create a starter dotbak configuration file."""

    config_path = config
    if config_path.exists() and not force:
        console.print(f"[red]Configuration '{config_path}' already exists. Use --force to overwrite.[/red]")
        raise typer.Exit(code=1)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_setting = f"{managed_root.rstrip('/')}/manifest.toml"

    discovered = _build_discovery(config_path.parent, discover)

    config_text = _render_init_config(
        managed_root=managed_root,
        manifest_path=manifest_setting,
        discovered=discovered,
    )

    config_path.write_text(config_text)
    console.print(f"[green]Created '{config_path}'.[/green]")

    if bootstrap_managed:
        _bootstrap_managed_dirs(config_path.parent, managed_root, discovered)


@app.command()
def apply(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to dotbak.toml"),
    group: list[str] = typer.Option(None, "--group", "-g", help="Limit to specific group(s)"),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip permission preflight checks (use with caution)",
    ),
) -> None:
    """Backup files into the managed directory and create symlinks."""

    try:
        manager = _load_manager(config)
        results = manager.apply(group or None, force=force)
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
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip permission preflight checks (use with caution)",
    ),
) -> None:
    """Replace symlinks with real files from the managed copies."""

    try:
        manager = _load_manager(config)
        results = manager.restore(group or None, forget=forget, force=force)
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

        perm_issues = manager.permission_issues(group or None)
        if perm_issues:
            console.print("[yellow]Permission preflight warnings:[/yellow]")
            _format_permission_issues(perm_issues)

        if has_issues or perm_issues:
            console.print(
                "[red]Issues detected. Resolve them or run 'dotbak apply --force' after reviewing warnings.[/red]"
            )
            raise typer.Exit(code=1)

        console.print("[green]All managed entries are healthy.[/green]")
    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


def run() -> None:
    """Entry point used for console_script bindings."""

    app()
