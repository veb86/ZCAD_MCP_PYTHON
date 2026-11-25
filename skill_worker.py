"""Child-process entry point that executes exactly one skill.

Started by :mod:`skill_runner` as ``python -I skill_worker.py`` (isolated mode:
no user site-packages, no ``PYTHON*`` env vars, no script dir on ``sys.path``).
It must stay self-contained: it does not import the server modules.

Protocol:
  * stdin  - one JSON request ``{skill_path, skill_name, dxf_path, params,
    workspace, skills_dir, memory_mb, hard_cap_bytes}``;
  * stdout - one JSON response (the skill result or ``{ok: false, error}``).
    Before the skill is imported, file descriptor 1 is redirected to stderr, so
    ``print()`` inside a skill can never corrupt the response (or the MCP stdio
    stream of the parent).

Guards (MVP, best effort - NOT a security sandbox; see README):
  * a ``sys.addaudithook`` hook denies network (``socket.*``), process creation
    (``subprocess``/``os.system``/``os.exec*``/``os.spawn*``/``os.fork``),
    ``ctypes`` and loading of forbidden modules;
  * file writes / deletes / renames are allowed only inside the workspace;
  * file reads are allowed only for the DXF, the workspace, the skills dir and
    the Python installation (stdlib / site-packages);
  * optional address-space limit (POSIX ``RLIMIT_AS``).
The parent process enforces the timeout by killing this process.
"""

from __future__ import annotations

import json
import os
import sys
import traceback

FORBIDDEN_IMPORTS = frozenset(
    {
        "socket", "_socket", "ssl", "_ssl", "select", "selectors", "asyncio",
        "subprocess", "_posixsubprocess", "multiprocessing", "concurrent",
        "ctypes", "_ctypes", "cffi",
        "urllib", "http", "httpx", "httpcore", "requests", "aiohttp", "urllib3",
        "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
        "socketserver", "webbrowser", "importlib", "runpy", "pty", "pexpect",
        "code", "codeop", "marshal", "builtins",
    }
)

DENIED_EVENTS_PREFIXES = ("socket.", "ctypes.", "winreg.", "_winapi.")
DENIED_EVENTS = frozenset(
    {
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
        "os.fork", "os.forkpty", "os.startfile", "os.kill", "os.killpg", "pty.spawn",
        "os.add_dll_directory", "webbrowser.open", "urllib.Request", "http.client.connect",
        "ftplib.connect", "smtplib.connect", "poplib.connect", "imaplib.open", "telnetlib.Telnet.open",
    }
)
# Events whose positional path arguments are destinations of a write.
WRITE_PATH_EVENTS = {
    "os.remove": (0,), "os.rmdir": (0,), "os.mkdir": (0,), "os.rename": (0, 1),
    "os.replace": (0, 1), "os.truncate": (0,), "os.chmod": (0,), "os.chown": (0,),
    "os.link": (0, 1), "os.symlink": (0, 1), "os.utime": (0,), "os.chflags": (0,),
    "os.lchmod": (0,), "os.lchown": (0,), "os.removexattr": (0,), "os.setxattr": (0,),
    "shutil.rmtree": (0,), "shutil.copyfile": (1,), "shutil.copytree": (1,),
    "shutil.move": (0, 1), "shutil.chown": (0,), "shutil.make_archive": (0,),
    "shutil.unpack_archive": (1,),
}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class SkillGuardError(PermissionError):
    pass


def _norm(path: object) -> str | None:
    if isinstance(path, int):  # already-open file descriptor
        return None
    if isinstance(path, bytes):
        path = os.fsdecode(path)
    try:
        path = os.fspath(path)  # type: ignore[arg-type]
    except TypeError:
        return None
    return os.path.normcase(os.path.realpath(os.path.abspath(path)))


def _within(path: str, roots: tuple[str, ...]) -> bool:
    for root in roots:
        if path == root or path.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def install_guard(workspace: str, read_roots: list[str], read_files: list[str]) -> None:
    write_roots = tuple(r for r in (_norm(workspace),) if r)
    readable_roots = tuple(r for r in (_norm(p) for p in read_roots) if r) + write_roots
    readable_files = frozenset(f for f in (_norm(p) for p in read_files) if f)

    def deny(what: str) -> None:
        raise SkillGuardError(f"Skill sandbox: {what} is not allowed")

    def check_write(path_obj: object, event: str) -> None:
        path = _norm(path_obj)
        if path is not None and not _within(path, write_roots):
            deny(f"{event} outside workspace ({path_obj!r})")

    def hook(event: str, args: tuple) -> None:
        if event in DENIED_EVENTS or event.startswith(DENIED_EVENTS_PREFIXES):
            deny(f"'{event}'")
        if event == "import":
            module = str(args[0]).split(".")[0] if args else ""
            if module in FORBIDDEN_IMPORTS:
                deny(f"import of '{args[0]}'")
            return
        if event == "open":
            path_obj, mode, flags = (tuple(args) + (None, None, None))[:3]
            is_write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
                isinstance(flags, int) and flags & _WRITE_FLAGS
            )
            if is_write:
                check_write(path_obj, "writing")
                return
            path = _norm(path_obj)
            if path is None or path in readable_files or _within(path, readable_roots):
                return
            deny(f"reading {path_obj!r} (only the DXF, workspace and skills are readable)")
        positions = WRITE_PATH_EVENTS.get(event)
        if positions:
            for index in positions:
                if index < len(args):
                    check_write(args[index], event)

    sys.addaudithook(hook)


def _to_jsonable(value: object) -> object:
    """``json.dumps(default=...)`` helper: numpy scalars/arrays, sets, tuples, paths."""
    if hasattr(value, "tolist"):
        return value.tolist()  # numpy arrays and scalars
    if hasattr(value, "item"):
        return value.item()
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=repr)
    if isinstance(value, os.PathLike):
        return os.fspath(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _set_memory_limit(memory_mb: int) -> None:
    if memory_mb <= 0:
        return
    try:
        import resource
    except ImportError:  # Windows
        return
    limit = memory_mb * 1024 * 1024
    try:
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ValueError, OSError):
        pass


def _error(message: str, **extra: object) -> dict:
    return {"ok": False, "error": message, **extra}


def main() -> int:
    raw_request = sys.stdin.buffer.read()
    # Keep the real stdout for the response; everything else goes to stderr.
    result_fd = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def respond(payload: dict, hard_cap: int) -> int:
        try:
            data = json.dumps(payload, ensure_ascii=False, default=_to_jsonable, allow_nan=False)
        except (TypeError, ValueError) as exc:
            data = json.dumps(_error(f"Skill result is not JSON-serializable: {exc}"))
        if len(data) > hard_cap:
            data = json.dumps(_error(f"Skill result is too large ({len(data)} bytes > {hard_cap})"))
        with os.fdopen(result_fd, "wb") as out:
            out.write(data.encode("utf-8"))
        return 0

    try:
        request = json.loads(raw_request.decode("utf-8"))
    except ValueError as exc:
        return respond(_error(f"Invalid worker request: {exc}"), 1 << 20)

    hard_cap = int(request.get("hard_cap_bytes") or 20 * 1024 * 1024)
    skill_path = request["skill_path"]
    dxf_path = request["dxf_path"]
    _set_memory_limit(int(request.get("memory_mb") or 0))

    # Load heavy libraries before the guard so their own file access is not restricted.
    for preload in ("ezdxf", "numpy"):
        try:
            __import__(preload)
        except ImportError:
            pass
    # Import modules that ezdxf loads lazily for common operations.
    try:
        import ezdxf.bbox  # noqa: F401
        import ezdxf.math  # noqa: F401
    except ImportError:
        pass

    import importlib.util as _ilu  # load before the guard forbids "importlib"

    read_roots = [request["workspace"], request["skills_dir"]]
    read_roots += [p for p in sys.path if p]
    read_roots += [sys.prefix, sys.base_prefix, sys.exec_prefix]
    install_guard(request["workspace"], read_roots, [dxf_path, skill_path])

    try:
        spec = _ilu.spec_from_file_location(f"zcad_skill_{request['skill_name']}", skill_path)
        if spec is None or spec.loader is None:
            return respond(_error(f"Cannot load skill from {skill_path}"), hard_cap)
        module = _ilu.module_from_spec(spec)
        spec.loader.exec_module(module)
        run = getattr(module, "run", None)
        if not callable(run):
            return respond(_error("Skill has no callable run(dxf_path, params)"), hard_cap)
        result = run(dxf_path, dict(request.get("params") or {}))
    except SkillGuardError as exc:
        return respond(_error(str(exc), error_type="SkillGuardError"), hard_cap)
    except MemoryError:
        return respond(_error("Skill exceeded the memory limit", error_type="MemoryError"), hard_cap)
    except BaseException as exc:  # noqa: BLE001 - report every skill failure
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        return respond(
            _error(f"{type(exc).__name__}: {exc}", error_type=type(exc).__name__, traceback="".join(tb[-3:])[-2000:]),
            hard_cap,
        )
    if not isinstance(result, dict):
        return respond(_error(f"run() must return a dict, got {type(result).__name__}"), hard_cap)
    return respond(result, hard_cap)


if __name__ == "__main__":
    sys.exit(main())
