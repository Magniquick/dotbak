"""Core package for the dotbak project."""

from .config import Config, GroupConfig, Settings
from .manager import DotbakError, DotbakManager
from .manifest import Manifest
from .models import (
    ApplyAction,
    ApplyResult,
    StatusEntry,
    StatusReport,
    StatusState,
)

__all__ = [
    "Config",
    "GroupConfig",
    "Settings",
    "DotbakManager",
    "DotbakError",
    "Manifest",
    "ApplyAction",
    "ApplyResult",
    "StatusEntry",
    "StatusReport",
    "StatusState",
]
