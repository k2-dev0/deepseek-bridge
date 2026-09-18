"""Private runtime configuration for noninteractive repository work."""

import os
import shlex
import shutil
import tempfile
from pathlib import Path

import yaml


def execution_patch(directory: Path) -> Path:
    """Configure the owned shell without changing the parent's environment."""
    git = shutil.which("git")
    if git is None:
        raise FileNotFoundError("Git executable is unavailable")
    executable = str(Path(git).resolve(strict=True))
    bindir = directory / "git-bin"
    if bindir.is_symlink():
        raise OSError("Linked runtime directory")
    bindir.mkdir(mode=0o700, exist_ok=True)
    bindir.chmod(0o700)
    if Path(executable) == bindir / "git":
        raise OSError("Git entrypoint must not resolve to itself")
    plugin = Path(__file__).with_suffix(".mjs")
    if not plugin.is_file():
        raise FileNotFoundError("Execution module is unavailable")
    rows = [
        {
            "id": "terminal-bash",
            # Persistent bash is interactive for its PTY protocol, but agent
            # commands are literal programs, never human history expressions.
            "config": {"shellArgs": ["--noprofile", "--norc", "-i", "+H"]},
        },
        {
            "insert": [
                {
                    "id": "bridge-execution-environment",
                    "name": str(plugin),
                    "config": {"bin": str(bindir)},
                }
            ]
        },
    ]
    outputs = [
        (bindir / "git", f'#!/bin/sh\nexec {shlex.quote(executable)} --no-pager "$@"\n', 0o700),
        (directory / "execution.cordis.yml", yaml.safe_dump(rows), 0o600),
    ]
    for destination, contents, mode in outputs:
        if destination.is_symlink():
            raise OSError("Linked runtime configuration")
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=destination.parent, delete=False) as f:
                temporary = Path(f.name)
                f.write(contents)
                os.fchmod(f.fileno(), mode)
            os.replace(temporary, destination)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return directory / "execution.cordis.yml"
