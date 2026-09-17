import ctypes
import os
import sys

import pytest
import yaml
from conftest import bridge


def test_other_platform_does_not_add_a_profile(tmp_path, monkeypatch):
    module = bridge("macos_process")
    monkeypatch.setattr(module.sys, "platform", "linux")
    assert module.runtime_patch(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS public process ABI")
def test_snapshot_contains_own_process_and_stable_start_identity():
    module = bridge("macos_process")
    assert ctypes.sizeof(module.BSDInfo) == 136
    first = {int(row.split()[0]): row.split()[1:] for row in module.query(["table"]).splitlines()}
    second = {int(row.split()[0]): row.split()[1:] for row in module.query(["table"]).splitlines()}
    assert first[os.getpid()] == second[os.getpid()]
    assert int(first[os.getpid()][0]) == os.getppid()
    assert len(first[os.getpid()][1].split(":")) == 2
    int(module.query(["foreground", str(os.getpid())]))
    for args in (["arbitrary"], ["foreground", "0"], ["foreground", "-1"]):
        with pytest.raises(ValueError):
            module.query(args)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS execution profile")
def test_private_profile_only_adds_process_compatibility(tmp_path):
    module = bridge("macos_process")
    path = module.runtime_patch(tmp_path)
    rows = yaml.safe_load(path.read_text())
    row = rows[0]["insert"][0]
    assert row["id"] == "bridge-macos-process-info"
    assert row["config"]["python"] == sys.executable
    assert row["config"]["helper"].endswith("macos_process.py")
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(rows) == len(rows[0]["insert"]) == 1
