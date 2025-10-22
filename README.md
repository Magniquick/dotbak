# dotbak 🗂️

## Overview
**dotbak** is a Python-powered dotfiles backup manager that mirrors your configuration files into a managed directory, preserving metadata and replacing originals with symlinks. It focuses on reliable backups over templating to keep your setup portable without surprises.

- Uses TOML for declarative configuration of base paths and entries.
- Preserves permissions, timestamps, and symlinks wherever the platform allows.
- Keeps a manifest for quick status checks and verification.

See **AGENTS.md** for the in-depth architecture plan and contributor notes.

## Features
- Group dotfiles under named base paths (e.g., `~/.config`) with simple TOML entries.
- Copy files into a managed tree while retaining metadata via `pathlib`-friendly utilities.
- Replace originals with symlinks only after successful backups.
- Planned commands for `init`, `apply`, `status`, `restore`, and `doctor` workflows.

## Requirements
- Python 3.13+
- [uv](https://github.com/astral-sh/uv) (recommended) for dependency and virtual environment management.

## Setup
1. Clone the repository and enter the project directory:
   ```sh
   git clone <repo-url>
   cd dotbak
   ```

2. Create the virtual environment and install dependencies:
   ```sh
   uv venv
   uv sync
   ```

> [!NOTE]
> uv will provision a `.venv` directory. Adjust your editor or shell to use it automatically.

## Configuration
Define your dotfiles in `dotbak.toml`:

```toml
[paths]
user_config = "~/.config"

[groups.user_config]
entries = [
  "zsh",
  "wezterm.lua",
]

[settings]
managed_root = "./managed"
manifest_path = "./managed/manifest.toml"
```

- `paths` maps logical group names to base directories.
- Each `groups.<name>` table lists relative paths beneath its base.
- `settings` configures where dotbak stores managed files and its manifest.

> [!IMPORTANT]
> Tests and tooling sandbox the home directory to prevent accidental writes outside the repository.

> [!CAUTION]
> Managing system paths (for example `/etc`) typically requires root privileges. When dotbak encounters entries that cannot be replaced with symlinks due to permissions, re-run the relevant command with elevated rights (e.g., `sudo dotbak apply`).

## Usage
Initialize a starter config:

```sh
dotbak init --config ./dotbak.toml
```

Add `--discover GROUP=PATH` (repeatable) to pre-populate entries from existing directories, and `--bootstrap-managed` to create the managed tree immediately.

Back up entries and create symlinks:

```sh
dotbak apply --config /path/to/dotbak.toml
```

Check the current state of managed entries:

```sh
dotbak status --config /path/to/dotbak.toml
```

Restore real files from managed copies (and optionally stop tracking them):

```sh
dotbak restore --config /path/to/dotbak.toml --forget
```

If a non-symlink already exists at the destination, dotbak preserves it beside the original as `<name>.dotbak-backup*` before restoring.

Run a health check that exits non-zero when issues are detected:

```sh
dotbak doctor --config /path/to/dotbak.toml
```

Pass `--group` multiple times to target specific groups. See `AGENTS.md` for the roadmap and deeper implementation notes.

## Testing
Run the full test suite with uv:

```sh
uv run pytest
```

Tests operate entirely within temporary directories, ensuring your actual dotfiles remain untouched.

## Development Notes
- Source code lives under `src/dotbak`; tests reside in `tests/`.
- Use `uv run pytest` and `uv run black .` for quick validation.
- Auto-formatting uses Black with `line-length = 120`; enable the git hook via `git config core.hooksPath .githooks`.
- Manifest entries capture ownership metadata when available. Restoring into privileged paths may require running commands with elevated privileges so ownership can be re-applied.

## Contributing
Contributions are welcome! Please open an issue or pull request with your ideas.

> [!TIP]
> Use `uv sync --dev` to install development dependencies before hacking on features or tests.

## License
Project licensing is TBD. Until finalized, treat the repository as all-rights-reserved.
