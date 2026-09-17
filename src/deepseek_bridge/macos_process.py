"""Read the two DSH macOS process queries through unprivileged libproc calls."""

import ctypes as c
import errno
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, ClassVar


class BSDInfo(c.Structure):
    # Public proc_bsdinfo ABI from Apple's sys/proc_info.h (136 bytes).
    _fields_: ClassVar[list[tuple[str, Any]]] = (
        [
            (name, c.c_uint32)
            for name in (
                "flags",
                "status",
                "xstatus",
                "pid",
                "ppid",
                "uid",
                "gid",
                "ruid",
                "rgid",
                "svuid",
                "svgid",
                "reserved",
            )
        ]
        + [("comm", c.c_char * 16), ("name", c.c_char * 32)]
        + [(name, c.c_uint32) for name in ("nfiles", "pgid", "jobc", "tdev", "tpgid")]
        + [("nice", c.c_int32), ("seconds", c.c_uint64), ("microseconds", c.c_uint64)]
    )


def query(arguments: list[str]) -> str:
    if sys.platform != "darwin" or c.sizeof(BSDInfo) != 136:
        raise ValueError("unsupported process information ABI")
    lib = c.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    lib.proc_listallpids.argtypes = [c.c_void_p, c.c_int]
    lib.proc_listallpids.restype = c.c_int
    lib.proc_pidinfo.argtypes = [c.c_int, c.c_int, c.c_uint64, c.c_void_p, c.c_int]
    lib.proc_pidinfo.restype = c.c_int

    def info(pid: int) -> BSDInfo | None:
        value = BSDInfo()
        c.set_errno(0)
        size = lib.proc_pidinfo(pid, 3, 0, c.byref(value), c.sizeof(value))
        if size == 0 and c.get_errno() in (errno.ESRCH, errno.EPERM, errno.EACCES):
            return None
        if size != c.sizeof(value):
            raise RuntimeError("process metadata unavailable")
        return value

    if arguments == ["table"]:
        count = lib.proc_listallpids(None, 0)
        if count <= 0:
            raise RuntimeError("process list unavailable")
        entries = (c.c_int * (count + 1024))()
        count = lib.proc_listallpids(entries, c.sizeof(entries))
        if count <= 0 or count >= len(entries):
            raise RuntimeError("process list incomplete")
        rows = []
        for pid in entries[:count]:
            value = info(pid)
            if value is not None:
                rows.append(value)
        if not any(value.pid == os.getppid() for value in rows):
            raise RuntimeError("own process tree cannot be inspected")
        return (
            "\n".join(
                f"{value.pid} {value.ppid} {value.seconds}:{value.microseconds}" for value in rows
            )
            + "\n"
        )
    if len(arguments) == 2 and arguments[0] == "foreground":
        pid = int(arguments[1])
        if not 0 < pid < 2**31:
            raise ValueError("invalid process identity")
        value = info(pid)
        return str(value.tpgid if value is not None else -1) + "\n"
    raise ValueError("unsupported process query")


def runtime_patch(directory: Path) -> Path | None:
    """Add an execution-only plugin; no tool, prompt, SDK binary or OS policy changes."""
    if sys.platform != "darwin":
        return None
    import yaml

    helper = Path(__file__).resolve()
    plugin = helper.with_suffix(".mjs")
    if not plugin.is_file():
        raise FileNotFoundError("macOS process compatibility module missing")
    patch = [
        {
            "insert": [
                {
                    "id": "bridge-macos-process-info",
                    "name": str(plugin),
                    "config": {"python": sys.executable, "helper": str(helper)},
                }
            ]
        }
    ]
    destination = directory / "macos-process.cordis.yml"
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(yaml.safe_dump(patch))
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return destination


if __name__ == "__main__":
    try:
        sys.stdout.write(query(sys.argv[1:]))
    except Exception:
        print("macOS process inspection failed", file=sys.stderr)
        raise SystemExit(1) from None
