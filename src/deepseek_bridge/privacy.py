"""Mandatory privacy profile and repository-isolated local state."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from platformdirs import user_state_path

from .protocol import COMMON_INSTRUCTIONS, BridgeError


@dataclass(frozen=True)
class StatePaths:
    home: Path
    runtime: Path


def prepare_state(workspace: Path) -> StatePaths:
    base = Path(user_state_path("deepseek-bridge", appauthor=False))
    digest = hashlib.sha256(os.fsencode(workspace.resolve())).hexdigest()
    if base.resolve().is_relative_to(workspace):
        raise BridgeError("configuration_error")
    home = base / "dsh-home" / digest
    runtime = base / "runtime" / digest
    try:
        for directory in (base, base / "dsh-home", base / "runtime", home, runtime):
            if directory.is_symlink():
                raise OSError("State directories must not be symlinks")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.chmod(0o700)
    except OSError:
        raise BridgeError("configuration_error") from None
    return StatePaths(home, runtime)


def privacy_patch() -> Path:
    packaged = Path(__file__).parent / "profiles" / "privacy.cordis.yml"
    source = Path(__file__).resolve().parents[2] / "profiles" / "privacy.cordis.yml"
    patch = packaged if packaged.is_file() else source
    try:
        rows = yaml.safe_load(patch.read_text())
        expected = {
            "session-log-deepseek": "@deepseek-ai/dsh-session-log-deepseek",
            "plugin-package-inventory-deepseek": (
                "@deepseek-ai/dsh-plugin-package-inventory-deepseek"
            ),
        }
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError("Invalid privacy profile")
        for identity, name in expected.items():
            matches = [row for row in rows if isinstance(row, dict) and row.get("id") == identity]
            if len(matches) != 1 or matches[0] != {
                "id": identity,
                "name": name,
                "config": {"enabled": False},
            }:
                raise ValueError("Invalid privacy row")
        if {"id": "llm-retry", "name": "@deepseek-ai/dsh-llm-retry", "disabled": True} not in rows:
            raise ValueError("Unbounded retries are forbidden")
    except (OSError, ValueError, yaml.YAMLError):
        raise BridgeError("privacy_configuration_error") from None
    return patch.resolve()


def child_environment() -> dict[str, str]:
    return {
        "DSH_TELEMETRY_MODE": "DISABLED",
        "DSH_TELEMETRY_DISABLED": "1",
        "OTEL_SDK_DISABLED": "true",
        "DSH_SYSTEM_PROMPT": COMMON_INSTRUCTIONS,
        "DSH_RUNTIME_MODE": "exe",
    }
