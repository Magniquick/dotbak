# dotbak Architecture Plan

## Objectives
- Provide a simple CLI for backing up dotfiles by mirroring them into a managed directory and symlinking originals back to the managed copies.
- Preserve file metadata (permissions, modified times, symlinks) so that backed-up copies are bit-for-bit equivalent to their sources whenever possible.
- Use TOML configuration to describe logical groups of dotfiles, each anchored at a base path (for example `user_config` → `~/.config`).
- Prefer Python's `pathlib` API for path handling to keep the implementation portable and expressive.
- Avoid templating or complex state tracking beyond what is needed to keep local files and their managed copies in sync.

## High-Level Flow
1. Load the project configuration (`dotbak.toml` by default) to obtain named groups and base paths.
2. For each group, expand paths, locate files or directories to manage, and compute their destination inside the repository (e.g. `managed/<group>/<relative-path>`).
3. Copy the dotfiles into the managed directory, preserving metadata (using `shutil.copy2`, `os.stat`, and `os.lchown` when available). Directories use `copytree(..., dirs_exist_ok=True, copy_function=shutil.copy2, symlinks=True)`.
4. After a successful copy, replace the original with a symbolic link pointing to the managed location (skipping if the link already exists and is correct).
5. Maintain a manifest that records each managed path, checksum, timestamps, and a snapshot of metadata for verification.
6. Offer commands to verify (`status`), back up (`apply`), restore originals (`restore`), and diff changes.

## Module Layout (planned)
- `dotbak.cli`: Typer-based CLI entry point that exposes commands (`init`, `apply`, `status`, `restore`, `doctor`).
- `dotbak.config`: Loads and validates the TOML config, expands environment variables, normalises paths, and exposes a typed model (using `pydantic` or `dataclasses`).
- `dotbak.manager`: Coordinates backup operations for all groups; orchestrates copying, linking, manifest updates, and error handling.
- `dotbak.filesystem`: Utilities for copying with metadata preservation, handling symlinks, and performing atomic writes (temporary file + replace).
- `dotbak.manifest`: Responsible for reading/writing the manifest (stored as TOML). Provides integrity checks (hashing with `hashlib.blake2b`).
- `dotbak.models`: Shared dataclasses/enums (e.g. `ManagedEntry`, `ManagedGroup`, `SyncResult`).

```
dotbak/
├── __init__.py
├── cli.py
├── config.py
├── manager.py
├── filesystem.py
├── manifest.py
└── models.py
```

## Configuration Shape
```toml
# dotbak.toml
[paths]
# logical name = base path
user_config = "~/.config"
system_dots = "/etc"

[groups.user_config]
entries = [
  "zsh",          # directory → ~/.config/zsh → managed/user_config/zsh
  "wezterm.lua",  # file → ~/.config/wezterm.lua → managed/user_config/wezterm.lua
]

[groups.system_dots]
entries = [
  "ssh/sshd_config",
]

[settings]
managed_root = "./managed"
manifest_path = "./managed/manifest.toml"
```

## Managed Directory Layout
- All synced files live under `<repo>/managed/<group>/<relative-path>`.
- Group directories mirror the source structure relative to their base path.
- A manifest file stores metadata and hashes for quick status checks.
- Temporary copies land in `<managed_root>/.staging` during atomic operations.

## Command Overview
- `init`: Create a skeleton config file (current implementation writes a commented template).
- `apply`: Copy files into the managed directory and convert originals into symlinks when safe.
- `status`: Compare manifest entries against current files, reporting drift (changed, missing, extra).
- `restore`: Replace symlinks with real files from managed copies; supports forgetting entries and pruning managed copies.
- `doctor`: Run sanity checks (valid symlinks, manifest consistency, writable destinations) and exit non-zero on issues.

## Metadata Strategy
- Prefer `shutil.copy2` and `os.lchmod`/`os.lchown` where supported to retain permission bits and ownership.
- Preserve symlink targets using `copytree(..., symlinks=True)` and `os.readlink` for manual handling.
- Record key metadata (mode, uid, gid, mtime) inside the manifest for verification.
- Optionally support extended attributes via `os.listxattr`/`os.setxattr` when available (guarded feature).
- Detect when entries reside in privileged locations (e.g., `/etc`) and surface actionable errors so the user can rerun commands with elevated privileges.

## Testing Approach
- Use `pytest` with `tmp_path` fixtures to simulate file trees and ensure operations behave correctly without touching real dotfiles.
- Employ `pytest-mock` to stub system interactions (e.g., `os.lchown` on platforms without support).
- Provide integration-style tests under `tests/integration/` that execute CLI commands via `typer.testing.CliRunner` against temporary directories.
- Add fixture helpers for creating fake configs and sample dotfiles to keep tests readable.

## Development Notes
- Source code lives under `src/dotbak`; tests reside in `tests/`.
- Run the test suite with `uv run pytest` to leverage the managed virtual environment.
- Configuration parsing is implemented in `src/dotbak/config.py` with Pydantic-based validation.
- Tests stub the home directory to a temporary path so no operations touch the real filesystem.

## Reference Material
- `agent_docs/pathlib.txt` contains a local copy of the Python `pathlib` documentation.
- `agent_docs/uv_quickstart.md` explains the Astral `uv` workflow used in this project.

## Next Implementation Steps
1. Enhance `init` with optional discovery (e.g., suggest common dotfiles) and managed directory bootstrapping.
2. Add privilege-awareness to the manager/CLI (clear messaging when operations require root) and extend automated tests to cover failure paths.
3. Extend manifest verification (checksum caching, pruning removed entries) and add integration tests that exercise CLI flows end-to-end (apply → status → restore).
4. Investigate platform-specific metadata support (ownership, extended attributes) behind feature flags.
