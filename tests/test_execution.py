import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from deepseek_bridge.execution import execution_patch


def test_private_git_entrypoint_preserves_arguments(tmp_path, monkeypatch):
    git = tmp_path / "real git"
    git.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    git.chmod(0o700)
    monkeypatch.setattr("deepseek_bridge.execution.shutil.which", lambda _: str(git))
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    patch = execution_patch(runtime)
    wrapper = runtime / "git-bin/git"
    result = subprocess.run(
        [str(wrapper), "--paginate", "argument with space", "quote'and\"quote"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.splitlines() == [
        "--no-pager",
        "--paginate",
        "argument with space",
        "quote'and\"quote",
    ]
    assert wrapper.stat().st_mode & 0o777 == 0o700
    assert patch.stat().st_mode & 0o777 == 0o600
    assert "+H" in yaml.safe_load(patch.read_text())[0]["config"]["shellArgs"]
    execution_patch(runtime)
    assert not list(runtime.glob("tmp*"))


def test_rejects_linked_git_entrypoint(tmp_path):
    target = tmp_path / "other"
    target.write_text("keep")
    (tmp_path / "git-bin").mkdir()
    (tmp_path / "git-bin/git").symlink_to(target)
    with pytest.raises(OSError):
        execution_patch(tmp_path)
    assert target.read_text() == "keep"


def test_plugin_preserves_environment_overrides_and_only_restores_git_names():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the execution plugin contract test")
    plugin = Path(__file__).resolve().parents[1] / "src/deepseek_bridge/execution.mjs"
    script = r"""
import assert from 'node:assert/strict';
const {apply} = await import(process.argv[1]);
const envBefore = {...process.env};
const service = {
  spawn(spec) { assert.equal(this, service); return spec; },
  spawnTerminal(spec) { assert.equal(this, service); return Promise.resolve(spec); },
};
const original = {...service};
let dispose;
apply({subprocess: service, on(event, cb) { assert.equal(event, 'dispose'); dispose = cb; }},
      {bin: '/private/bin'});
for (const method of ['spawn', 'spawnTerminal']) {
  const spec = {argv: ['program', 'arg with space'], env: {EXPLICIT: 'value'}};
  const result = await service[method](spec);
  assert.equal(result.argv, spec.argv);
  assert.deepEqual(spec.env, {EXPLICIT:'value'});
  assert.equal(result.env.GIT_CONFIG_KEY_0, 'core.fsmonitor');
  assert.equal(result.env.GIT_CONFIG_KEY_1, undefined); // Missing remains missing.
  assert.equal(result.env.GIT_CONFIG_KEY_2, undefined); // Outside count.
  assert.equal(result.env.WIRE_SECRET, undefined);
  assert.equal(result.env.WIRE_OTHER_KEY, undefined);
  assert.equal(result.env.PATH, '/private/bin:' + process.env.PATH);
  const overrides = await service[method]({env:{
    GIT_CONFIG_COUNT:'1', GIT_CONFIG_KEY_0:undefined, PATH:undefined,
  }});
  assert.equal(overrides.env.GIT_CONFIG_KEY_0, undefined);
  assert.equal(overrides.env.PATH, undefined);
  const explicit = await service[method]({env:{GIT_CONFIG_KEY_0:'core.hooksPath'}});
  assert.equal(explicit.env.GIT_CONFIG_KEY_0, 'core.hooksPath');
  for (const count of ['0', 'invalid', '999999999999999999999999']) {
    const limited = await service[method]({env:{GIT_CONFIG_COUNT:count}});
    assert.equal(limited.env.GIT_CONFIG_KEY_0, undefined);
  }
}
assert.deepEqual({...process.env}, envBefore);
dispose();
assert.equal(service.spawn, original.spawn);
assert.equal(service.spawnTerminal, original.spawnTerminal);
"""
    # Never pass real credentials to assertion-based diagnostic subprocesses.
    env = {"PATH": os.defpath}
    env.update(
        {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_KEY_2": "core.hooksPath",
            "WIRE_SECRET": "fixture-only",
            "WIRE_OTHER_KEY": "fixture-only",
        }
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script, str(plugin)],
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
