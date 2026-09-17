"""Run real SDK wire tests with OS-enforced loopback-only networking."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
command = [
    sys.executable,
    "-m",
    "pytest",
    "-m",
    "wire",
    "--tb=short",
    "-q",
    "-s",
    "--basetemp",
    "/private/tmp/deepseek-bridge-wire"
    if sys.platform == "darwin"
    else "/tmp/deepseek-bridge-wire",
]
if os.environ.get("BRIDGE_WIRE_SANDBOX_OUTER") == "1":
    pass  # The host wrapper applies one combined Git + loopback network policy.
elif sys.platform == "darwin":
    policy = """(version 1)
(allow default)
(deny network*)
(allow network-bind (local ip "localhost:*"))
(allow network-inbound (local ip "localhost:*"))
(allow network-outbound (remote ip "localhost:*"))
"""
    command = ["/usr/bin/sandbox-exec", "-p", policy, *command]
elif sys.platform.startswith("linux") and shutil.which("bwrap"):
    command = ["bwrap", "--die-with-parent", "--unshare-net", "--bind", "/", "/", "--", *command]
else:
    raise SystemExit("Privacy egress boundary UNVERIFIED: sandbox-exec or Linux bwrap is required")
result = subprocess.run(command, cwd=root, env={**os.environ, "BRIDGE_WIRE_SANDBOX": "1"})
raise SystemExit(result.returncode)
