"""Command-line interface for dotbak."""

from __future__ import annotations

import io
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

import tomli_w
import typer
from rich.console import Console
from rich.table import Table

from .config import DEFAULT_CONFIG_FILENAME, DEFAULT_CONFIG_PATH, Config, ConfigError, GroupConfig, load_config
from .manager import DotbakError, DotbakManager
from .models import (
    ApplyConflict,
    ApplyResolution,
    ApplyResult,
    ManagedPath,
    RestoreAction,
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
    from .config import ConfigError
    from .manager import DotbakError

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
        StatusState.METADATA_DIFFER: "red",
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


def _interactive_collect_groups(config_dir: Path) -> list[DiscoveredGroup]:
    console.print("[cyan]Interactive configuration wizard[/cyan]")
    console.print("Press Enter to accept defaults. Entries should be relative paths beneath the base directory.")

    groups: list[DiscoveredGroup] = []
    while True:
        default_name = "dotfiles" if not groups else f"group_{len(groups) + 1}"
        group_name = typer.prompt("Group name", default=default_name)
        base_input = typer.prompt("Base path", default=str(Path.home()))
        resolved_base = _resolve_discovery_path(base_input, config_dir)

        entries_input = typer.prompt(
            "Entries (comma separated, leave blank for none)",
            default="",
        ).strip()
        entries = [entry.strip() for entry in entries_input.split(",") if entry.strip()]

        groups.append(
            DiscoveredGroup(
                name=_sanitize_group_name(group_name),
                raw_path=base_input,
                resolved_path=resolved_base,
                entries=entries,
            )
        )

        if not typer.confirm("Add another group?", default=False):
            break

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
        "groups": {group.name: {"base": group.raw_path, "entries": group.entries} for group in discovered},
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


def _load_config_data(config_path: Path) -> dict:
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def _config_header_block(config_path: Path) -> str:
    text = config_path.read_text()
    lines = text.splitlines(keepends=True)
    prefix: list[str] = []
    encountered_comment = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#"):
            prefix.append(line)
            encountered_comment = True
            continue
        if stripped == "" and encountered_comment:
            prefix.append(line)
            continue
        break

    return "".join(prefix)


def _resolve_target_path(raw: Path | str) -> Path:
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    if candidate.exists() and candidate.is_symlink():
        return candidate.absolute()
    return candidate.resolve(strict=False)


def _prompt_apply_conflict(conflict: ApplyConflict) -> ApplyResolution:
    console.print("[yellow]Merge conflict detected during apply:[/yellow]")
    console.print(f"  Group : {conflict.group}")
    console.print(f"  Entry : {conflict.entry.as_posix()}")
    console.print(f"  Source : {conflict.source_path}")
    console.print(f"  Managed: {conflict.managed_path}")
    console.print("Choose how to proceed: [s]ystem (use current system copy), [m]anaged (keep managed copy), [a]bort")

    while True:
        choice = typer.prompt("Resolution", default="s").strip().lower()
        if choice in {"s", "system"}:
            return ApplyResolution.USE_SYSTEM
        if choice in {"m", "managed"}:
            return ApplyResolution.USE_MANAGED
        if choice in {"a", "abort"}:
            return ApplyResolution.ABORT
        console.print("[red]Invalid choice. Enter 's', 'm', or 'a'.[/red]")


def _matching_groups(config: Config, target: Path) -> list[GroupConfig]:
    resolved = _resolve_target_path(target)

    matches: list[GroupConfig] = []
    for group in config.groups.values():
        try:
            resolved.relative_to(group.base_path)
        except ValueError:
            continue
        matches.append(group)
    return matches


def _pick_group(candidates: Sequence[GroupConfig], target: Path) -> GroupConfig:
    console.print(f"[yellow]Multiple groups match '{target}':[/yellow]")
    for index, group in enumerate(candidates, start=1):
        console.print(f"  {index}. {group.name} (base: {group.base_path})")

    while True:
        selection = typer.prompt("Select group", type=int, default=1)
        if 1 <= selection <= len(candidates):
            return candidates[selection - 1]
        console.print(f"[red]Invalid selection '{selection}'. Choose between 1 and {len(candidates)}.[/red]")


@app.command()
def init(
    config: Path = typer.Option(
        DEFAULT_CONFIG_PATH,
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
    interactive: bool = typer.Option(
        False,
        "--interactive/--no-interactive",
        help="Prompt for groups and entries interactively",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing config if present"),
) -> None:
    """Create a starter dotbak configuration file."""

    config_path = config
    if config_path.exists() and not force:
        console.print(f"[red]Configuration '{config_path}' already exists. Use --force to overwrite.[/red]")
        raise typer.Exit(code=1)

    if interactive and discover:
        console.print("[red]--interactive cannot be combined with --discover.[/red]")
        raise typer.Exit(code=1)

    config_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_setting = f"{managed_root.rstrip('/')}/manifest.toml"

    if interactive:
        discovered = _interactive_collect_groups(config_path.parent)
    else:
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
def add(
    paths: list[str] = typer.Argument(..., help="Source files or directories to start managing"),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to dotbak.toml"),
) -> None:
    """Register new entries in the configuration file."""

    try:
        config_obj = load_config(config)
        raw_data = _load_config_data(config_obj.config_path)
        groups_table = raw_data.get("groups")
        if not isinstance(groups_table, dict):
            raise ConfigError("Configuration is missing a [groups] table")

        additions: list[tuple[str, str, Path]] = []
        errors: list[str] = []

        for raw in paths:
            resolved = _resolve_target_path(raw)
            matches = _matching_groups(config_obj, resolved)
            if not matches:
                errors.append(f"No configured group base covers '{resolved}'.")
                continue

            max_specificity = max(len(group.base_path.parts) for group in matches)
            candidates = [group for group in matches if len(group.base_path.parts) == max_specificity]
            chosen = candidates[0] if len(candidates) == 1 else _pick_group(candidates, resolved)

            try:
                relative = resolved.relative_to(chosen.base_path)
            except ValueError:
                errors.append(f"Path '{resolved}' is not inside base '{chosen.base_path}'.")
                continue

            if str(relative) in {".", ""}:
                errors.append(
                    f"Path '{resolved}' refers to the group base '{chosen.base_path}'. Choose a child entry instead."
                )
                continue

            entry_text = relative.as_posix()

            group_section = groups_table.get(chosen.name)
            if not isinstance(group_section, dict):
                errors.append(f"Group '{chosen.name}' is missing from configuration data.")
                continue

            entries = list(group_section.get("entries") or [])
            if entry_text in entries:
                console.print(
                    f"[yellow]Entry '{entry_text}' is already tracked in group '{chosen.name}'. Skipping.[/yellow]"
                )
                continue

            entries.append(entry_text)
            entries.sort()
            group_section["entries"] = entries
            additions.append((chosen.name, entry_text, resolved))

        if additions:
            header = _config_header_block(config_obj.config_path)
            serialized = tomli_w.dumps(raw_data)
            new_body = f"{header}{serialized}" if header else serialized
            config_obj.config_path.write_text(new_body)
            for group_name, entry_text, resolved in additions:
                console.print(f"[green]Added '{resolved}' as '{entry_text}' in group '{group_name}'.[/green]")

        if errors:
            for message in errors:
                console.print(f"[red]{message}[/red]")
            raise typer.Exit(code=1 if not additions else 0)

        if not additions and not errors:
            console.print("[yellow]No changes were made to the configuration.[/yellow]")

    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


@app.command()
def remove(
    paths: list[str] = typer.Argument(..., help="Managed paths to stop tracking"),
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to dotbak.toml"),
    force: bool = typer.Option(False, "--force", help="Skip writable checks when restoring"),
) -> None:
    """Remove entries from the configuration and restore real files from managed copies."""

    try:
        manager = _load_manager(config)
        config_obj = manager.config
        raw_data = _load_config_data(config_obj.config_path)
        groups_table = raw_data.get("groups")
        if not isinstance(groups_table, dict):
            raise ConfigError("Configuration is missing a [groups] table")

        removals: list[tuple[str, str, Path]] = []
        errors: list[str] = []
        removal_requests: list[tuple[GroupConfig, Path]] = []
        removal_details: list[tuple[str, str, Path]] = []

        for raw in paths:
            resolved = _resolve_target_path(raw)
            matches = _matching_groups(config_obj, resolved)
            if not matches:
                errors.append(f"No configured group base covers '{resolved}'.")
                continue

            max_specificity = max(len(group.base_path.parts) for group in matches)
            candidates = [group for group in matches if len(group.base_path.parts) == max_specificity]
            chosen = candidates[0] if len(candidates) == 1 else _pick_group(candidates, resolved)

            try:
                relative = resolved.relative_to(chosen.base_path)
            except ValueError:
                errors.append(f"Path '{resolved}' is not inside base '{chosen.base_path}'.")
                continue

            entry_text = relative.as_posix()
            group_section = groups_table.get(chosen.name)
            if not isinstance(group_section, dict):
                errors.append(f"Group '{chosen.name}' is missing from configuration data.")
                continue

            entries = list(group_section.get("entries") or [])
            if entry_text not in entries:
                console.print(
                    f"[yellow]Entry '{entry_text}' is not tracked in group '{chosen.name}'. Skipping.[/yellow]"
                )
                continue

            entry_path = Path(entry_text)
            group_cfg = manager.config.group(chosen.name)
            removal_requests.append((group_cfg, entry_path))
            removal_details.append((chosen.name, entry_text, resolved))

            entries.remove(entry_text)
            if entries:
                group_section["entries"] = entries
            else:
                groups_table.pop(chosen.name, None)
                paths_table = raw_data.get("paths")
                if isinstance(paths_table, dict):
                    paths_table.pop(chosen.name, None)

        results = manager.remove_entries(removal_requests, force=force)
        for (group_name, entry_text, resolved), restore_result in zip(removal_details, results, strict=False):
            if restore_result.action is not RestoreAction.RESTORED:
                console.print(
                    f"[yellow]Restore for '{entry_text}' in group '{group_name}' reported '{restore_result.action.value}'.[/yellow]"
                )
            removals.append((group_name, entry_text, resolved))

        for message in manager.pull_warnings():
            console.print(f"[yellow]Warning:[/yellow] {message}")

        if removals:
            if groups_table:
                header = _config_header_block(config_obj.config_path)
                serialized = tomli_w.dumps(raw_data)
                new_body = f"{header}{serialized}" if header else serialized
                config_obj.config_path.write_text(new_body)
            else:
                config_obj.config_path.unlink(missing_ok=True)
                console.print(
                    "[yellow]All groups removed; configuration file deleted because no entries remain.[/yellow]"
                )

            for group_name, entry_text, resolved in removals:
                console.print(f"[green]Removed '{resolved}' (entry '{entry_text}') from group '{group_name}'.[/green]")

        if errors:
            for message in errors:
                console.print(f"[red]{message}[/red]")
            raise typer.Exit(code=1 if not removals else 0)

        if not removals and not errors:
            console.print("[yellow]No changes were made to the configuration.[/yellow]")

    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


@app.command()
def orphan(
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to dotbak.toml"),
    prune: bool = typer.Option(False, "--prune", help="Delete managed copies and manifest entries"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not prompt for confirmation when pruning"),
) -> None:
    """Inspect or prune manifest entries that no longer appear in the config."""

    try:
        manager = _load_manager(config)
        orphans = manager.list_orphans()
        if not orphans:
            console.print("[green]No orphaned manifest entries found.[/green]")
            return

        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("Group")
        table.add_column("Entry")
        table.add_column("Managed Path", overflow="fold")

        root = manager.config.settings.managed_root
        style = manager.config.settings.dot_prefix_style
        for entry in orphans:
            table.add_row(
                entry.path.group,
                entry.path.relative_path.as_posix(),
                entry.managed_path(root, style).as_posix(),
            )

        console.print(table)

        if not prune:
            console.print(
                "[yellow]Use 'dotbak orphan --prune' to remove these managed copies if they are no longer needed.[/yellow]"
            )
            raise typer.Exit(code=1)

        if not yes and not typer.confirm("Prune the listed orphaned entries?", default=False):
            console.print("[yellow]Aborted without pruning orphans.[/yellow]")
            raise typer.Exit(code=1)

        pruned = manager.prune_orphans()
        console.print(f"[green]Pruned {len(pruned)} orphaned entries.[/green]")
    except Exception as exc:  # noqa: BLE001
        _handle_error(exc)


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
        results = manager.apply(group or None, force=force, resolver=_prompt_apply_conflict)
        _format_apply_results(results)
        for message in manager.pull_warnings():
            console.print(f"[yellow]Warning:[/yellow] {message}")
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
        for message in manager.pull_warnings():
            console.print(f"[yellow]Warning:[/yellow] {message}")
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
        git_root = getattr(manager, "git_root", None)
        if git_root is None:
            console.print(
                "[yellow]Warning: configuration directory is not inside a Git repository; dotbak will fall back to file hashes for drift detection.[/yellow]"
            )
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
