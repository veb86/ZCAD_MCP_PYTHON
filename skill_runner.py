"""Loading, validation and execution of DXF analysis skills.

A skill is ``skills/<name>.py`` exposing ``SKILL`` (metadata dict) and
``run(dxf_path: str, params: dict) -> dict`` (see :mod:`skill_template`).

* Listing / reading skills never executes their code: metadata is extracted
  statically with :mod:`ast`.
* Saving a skill validates the name, the syntax, the presence of ``run`` and a
  denylist of dangerous imports/calls (network, subprocess, ctypes, eval...).
* Running a skill happens in a separate ``python -I skill_worker.py`` process
  with a timeout (the process is killed), runtime guards (see
  :mod:`skill_worker`) and post-processing that keeps the answer compact:
  every list longer than ``max_segments`` is truncated (with a warning) and
  the serialized answer is limited to ``max_result_bytes``.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import Settings

log = logging.getLogger("zcad_mcp.skills")

SKILL_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
MAX_SKILL_NAME_LEN = 64
MAX_SOURCE_BYTES = 256 * 1024
WORKER_PATH = Path(__file__).resolve().parent / "skill_worker.py"

# Keys of the skill answer that are never truncated.
RESERVED_KEYS = ("ok", "skill", "error", "stats", "warnings", "truncated", "elapsed_sec")

FORBIDDEN_IMPORTS = frozenset(
    {
        "socket", "_socket", "ssl", "_ssl", "select", "selectors", "asyncio",
        "subprocess", "_posixsubprocess", "multiprocessing", "concurrent",
        "ctypes", "_ctypes", "cffi",
        "urllib", "http", "httpx", "httpcore", "requests", "aiohttp", "urllib3",
        "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
        "socketserver", "webbrowser", "importlib", "runpy", "pty", "pexpect",
        "code", "codeop", "marshal", "builtins",
        # server modules: skills must not drive ZCAD or the runner directly
        "server", "zcad_client", "skill_runner", "skill_worker", "config", "dxf_workspace",
    }
)
FORBIDDEN_CALLS = frozenset({"eval", "exec", "compile", "__import__", "breakpoint"})
FORBIDDEN_OS_ATTRS = re.compile(r"^(system|popen|exec\w*|spawn\w*|posix_spawn\w*|fork\w*|kill\w*|startfile|add_dll_directory)$")
FORBIDDEN_DUNDER_ATTRS = frozenset(
    {"__builtins__", "__subclasses__", "__globals__", "__code__", "__closure__", "__loader__", "__spec__", "__import__", "__dict__", "__bases__", "__mro__"}
)


class SkillError(ValueError):
    """Invalid skill name / source / request."""


@dataclass
class SourceCheck:
    errors: list[str]
    warnings: list[str]
    skill_meta: dict[str, Any] | None
    docstring: str | None


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not SKILL_NAME_RE.match(name):
        raise SkillError(f"Invalid skill name {name!r}: expected ^[a-zA-Z_][a-zA-Z0-9_]*$")
    if len(name) > MAX_SKILL_NAME_LEN:
        raise SkillError(f"Skill name is longer than {MAX_SKILL_NAME_LEN} characters")
    if name.startswith("__"):
        raise SkillError("Skill names starting with '__' are reserved")
    return name


def check_source(source: str, name: str | None = None) -> SourceCheck:
    """Static checks of a skill source (no code is executed)."""
    errors: list[str] = []
    warnings: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return SourceCheck([f"SyntaxError: {exc.msg} (line {exc.lineno})"], [], None, None)

    run_def = None
    skill_meta: dict[str, Any] | None = None
    skill_assigned = False
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            run_def = node
        elif isinstance(node, ast.AsyncFunctionDef) and node.name == "run":
            errors.append("run() must be a regular (not async) function")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id == "SKILL" for t in targets) and node.value is not None:
                skill_assigned = True
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:
                    warnings.append("SKILL is not a literal dict; list_skills cannot read its metadata")
                else:
                    if isinstance(value, dict):
                        skill_meta = value
                    else:
                        errors.append("SKILL must be a dict")

    if run_def is None:
        errors.append("Skill must define a top-level function run(dxf_path, params)")
    else:
        positional = run_def.args.posonlyargs + run_def.args.args
        if len(positional) < 2 and run_def.args.vararg is None:
            errors.append("run() must accept two positional arguments: (dxf_path, params)")
    if not skill_assigned:
        warnings.append("SKILL metadata dict is missing (recommended: name, description, params)")
    if skill_meta is not None:
        skill_name = skill_meta.get("name")
        if name is not None and skill_name is not None and skill_name != name:
            errors.append(f"SKILL['name'] is {skill_name!r} but the skill file name is {name!r}")
        if "params" in skill_meta and not isinstance(skill_meta["params"], dict):
            errors.append("SKILL['params'] must be a dict of default values")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    errors.append(f"line {node.lineno}: import of '{alias.name}' is not allowed in skills")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                errors.append(f"line {node.lineno}: relative imports are not allowed in skills")
            elif node.module and node.module.split(".")[0] in FORBIDDEN_IMPORTS:
                errors.append(f"line {node.lineno}: import from '{node.module}' is not allowed in skills")
            elif node.module == "os" and any(FORBIDDEN_OS_ATTRS.match(a.name) for a in node.names):
                errors.append(f"line {node.lineno}: process-spawning functions from 'os' are not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_CALLS:
            errors.append(f"line {node.lineno}: '{node.id}' is not allowed in skills")
        elif isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_DUNDER_ATTRS:
                errors.append(f"line {node.lineno}: access to '{node.attr}' is not allowed in skills")
            elif isinstance(node.value, ast.Name) and node.value.id == "os" and FORBIDDEN_OS_ATTRS.match(node.attr):
                errors.append(f"line {node.lineno}: os.{node.attr} is not allowed in skills (no subprocesses)")

    return SourceCheck(errors, warnings, skill_meta, ast.get_docstring(tree))


_JSON_TYPES = ((bool, "boolean"), (int, "integer"), (float, "number"), (str, "string"), (list, "array"), (tuple, "array"), (dict, "object"))


def params_schema_from_defaults(params: dict[str, Any]) -> dict[str, Any]:
    """Derive a JSON schema from ``SKILL['params']`` default values."""
    properties: dict[str, Any] = {}
    for key, default in params.items():
        prop: dict[str, Any] = {"default": list(default) if isinstance(default, tuple) else default}
        for py_type, json_type in _JSON_TYPES:
            if isinstance(default, py_type):
                prop["type"] = json_type
                break
        properties[str(key)] = prop
    return {"type": "object", "properties": properties}


def _json_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def compact_result(result: Any, name: str, max_items: int, max_bytes: int) -> dict[str, Any]:
    """Normalize a skill answer and enforce the size policy.

    * not a dict -> ``{ok: false}``;
    * fills ``ok`` / ``skill`` / ``warnings``;
    * every list (outside ``stats``/``warnings``) longer than ``max_items`` is cut to
      ``max_items``; ``truncated`` maps the JSON path to the original length;
    * if the JSON is still larger than ``max_bytes`` the geometry is dropped and an
      error is returned together with ``stats``.
    """
    if not isinstance(result, dict):
        return {"ok": False, "skill": name, "error": f"Skill returned {type(result).__name__}, expected dict"}
    out = dict(result)
    out.setdefault("ok", "error" not in out)
    out["skill"] = out.get("skill") or name
    warnings = out.get("warnings")
    warnings = [str(w) for w in warnings] if isinstance(warnings, (list, tuple)) else ([str(warnings)] if warnings else [])
    truncated: dict[str, int] = {}

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, dict):
            return {k: walk(v, f"{path}.{k}") for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) > max_items:
                truncated[path] = len(items)
                items = items[:max_items]
            return [walk(v, f"{path}[]") for v in items]
        return value

    for key in list(out):
        if key not in RESERVED_KEYS:
            out[key] = walk(out[key], key)
    if truncated:
        for path, original in truncated.items():
            warnings.append(f"{path}: truncated from {original} to {max_items} items (max_segments)")
        out["truncated"] = truncated
    out["warnings"] = warnings

    size = _json_size(out)
    if size > max_bytes:
        return {
            "ok": False,
            "skill": out["skill"],
            "error": (
                f"Skill result is too large ({size} bytes > {max_bytes}). Return aggregates and "
                "fewer candidates (respect params['max_segments'])."
            ),
            "stats": out.get("stats") if _json_size(out.get("stats")) <= max_bytes // 4 else None,
            "warnings": warnings[:20],
        }
    return out


class SkillRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.skills_dir = settings.skills_dir
        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------ paths
    def skill_path(self, name: str) -> Path:
        return self.skills_dir / f"{validate_name(name)}.py"

    def _meta_path(self, name: str) -> Path:
        return self.skills_dir / f"{validate_name(name)}.meta.json"

    def _read_meta(self, name: str) -> dict[str, Any]:
        try:
            data = json.loads(self._meta_path(name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    # ---------------------------------------------------------------- listing
    def describe(self, name: str) -> dict[str, Any]:
        path = self.skill_path(name)
        if not path.is_file():
            raise SkillError(f"Skill {name!r} not found")
        source = path.read_text(encoding="utf-8")
        check = check_source(source, name)
        meta = check.skill_meta or {}
        sidecar = self._read_meta(name)
        description = sidecar.get("description") or meta.get("description") or (check.docstring or "").strip().split("\n")[0]
        if isinstance(meta.get("params_schema"), dict):
            schema = meta["params_schema"]
        else:
            schema = params_schema_from_defaults(meta.get("params") if isinstance(meta.get("params"), dict) else {})
        info: dict[str, Any] = {"name": name, "description": description, "params_schema": schema}
        if check.errors:
            info["errors"] = check.errors
        return info

    def list_skills(self) -> list[dict[str, Any]]:
        items = []
        if not self.skills_dir.is_dir():
            return items
        for path in sorted(self.skills_dir.glob("*.py")):
            name = path.stem
            if not SKILL_NAME_RE.match(name) or name.startswith("__"):
                continue
            try:
                items.append(self.describe(name))
            except (OSError, SkillError, UnicodeDecodeError) as exc:
                items.append({"name": name, "description": "", "params_schema": {"type": "object"}, "errors": [str(exc)]})
        return items

    def get_source(self, name: str) -> dict[str, Any]:
        info = self.describe(name)
        return {"name": name, "description": info["description"], "source": self.skill_path(name).read_text(encoding="utf-8")}

    # ---------------------------------------------------------------- writing
    def save(self, name: str, source_code: str, description: str = "", overwrite: bool = False) -> dict[str, Any]:
        validate_name(name)
        if not isinstance(source_code, str) or not source_code.strip():
            raise SkillError("source_code is empty")
        if len(source_code.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise SkillError(f"source_code is larger than {MAX_SOURCE_BYTES} bytes")
        check = check_source(source_code, name)
        if check.errors:
            return {"ok": False, "name": name, "error": "Skill validation failed", "errors": check.errors, "warnings": check.warnings}
        path = self.skill_path(name)
        with self._write_lock:
            self.skills_dir.mkdir(parents=True, exist_ok=True)
            existed = path.exists()
            if existed and not overwrite:
                return {
                    "ok": False,
                    "name": name,
                    "error": f"Skill {name!r} already exists; pass overwrite=true to replace it",
                }
            tmp = path.with_name(f".{name}.py.tmp")
            tmp.write_text(source_code, encoding="utf-8", newline="\n")
            tmp.replace(path)
            now = time.time()
            meta = self._read_meta(name) if existed else {}
            meta.update(
                {
                    "description": description.strip() or (check.skill_meta or {}).get("description") or "",
                    "sha256": hashlib.sha256(source_code.encode("utf-8")).hexdigest(),
                    "updated_at": now,
                }
            )
            meta.setdefault("created_at", now)
            self._meta_path(name).write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("skill %s %s at %s", name, "updated" if existed else "created", path)
        return {"ok": True, "name": name, "path": str(path), "overwritten": existed, "warnings": check.warnings}

    def delete(self, name: str) -> dict[str, Any]:
        path = self.skill_path(name)
        with self._write_lock:
            if not path.is_file():
                return {"ok": False, "name": name, "error": f"Skill {name!r} not found"}
            path.unlink()
            self._meta_path(name).unlink(missing_ok=True)
        log.info("skill %s deleted", name)
        return {"ok": True, "name": name, "path": str(path)}

    # ---------------------------------------------------------------- running
    def run(
        self,
        name: str,
        dxf_path: Path,
        params: dict[str, Any] | None = None,
        max_segments: int | None = None,
    ) -> dict[str, Any]:
        """Execute a skill in a worker process and return the compact answer."""
        started = time.monotonic()
        try:
            path = self.skill_path(name)
        except SkillError as exc:
            return {"ok": False, "skill": name, "error": str(exc)}
        if not path.is_file():
            return {"ok": False, "skill": name, "error": f"Skill {name!r} not found (see list_skills)"}
        if not dxf_path.is_file():
            return {"ok": False, "skill": name, "error": f"DXF file not found: {dxf_path}"}
        if params is not None and not isinstance(params, dict):
            return {"ok": False, "skill": name, "error": "params must be a JSON object"}

        check = check_source(path.read_text(encoding="utf-8"), name)
        if check.errors:
            return {"ok": False, "skill": name, "error": "Skill validation failed", "errors": check.errors}

        params = dict(params or {})
        limit = max_segments if max_segments is not None else params.get("max_segments", self.settings.max_segments)
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            return {"ok": False, "skill": name, "error": "max_segments must be an integer"}
        if limit < 1:
            return {"ok": False, "skill": name, "error": "max_segments must be >= 1"}
        params["max_segments"] = limit

        log.info("run_skill %s dxf=%s params=%s", name, dxf_path, sorted(params))
        raw = self._execute(name, path, dxf_path, params)
        if raw.get("ok") is False and "error" in raw and set(raw) <= {"ok", "error", "error_type", "traceback", "stderr"}:
            raw["skill"] = name
            answer = raw
        else:
            answer = compact_result(raw, name, limit, self.settings.max_result_bytes)
        answer["elapsed_sec"] = round(time.monotonic() - started, 3)
        log.info("run_skill %s finished ok=%s in %.3fs", name, answer.get("ok"), answer["elapsed_sec"])
        return answer

    def _execute(self, name: str, path: Path, dxf_path: Path, params: dict[str, Any]) -> dict[str, Any]:
        workspace = self.settings.workspace_dir
        workspace.mkdir(parents=True, exist_ok=True)
        request = {
            "skill_name": name,
            "skill_path": str(path.resolve()),
            "dxf_path": str(dxf_path.resolve()),
            "params": params,
            "workspace": str(workspace.resolve()),
            "skills_dir": str(self.skills_dir.resolve()),
            "memory_mb": self.settings.skill_memory_mb,
            "hard_cap_bytes": max(self.settings.max_result_bytes * 50, 5 * 1024 * 1024),
        }
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ZCAD_", "PYTHON"))}
        timeout = self.settings.skill_timeout_sec
        with tempfile.TemporaryFile() as stderr_file:
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", str(WORKER_PATH)],
                    input=json.dumps(request).encode("utf-8"),
                    stdout=subprocess.PIPE,
                    stderr=stderr_file,
                    cwd=str(workspace),
                    env=env,
                    timeout=timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                log.warning("skill %s timed out after %ss", name, timeout)
                return {"ok": False, "error": f"Skill timed out after {timeout:g} s (ZCAD_MCP_SKILL_TIMEOUT_SEC)"}
            except OSError as exc:
                return {"ok": False, "error": f"Cannot start skill worker: {exc}"}
            stderr_file.seek(0, os.SEEK_END)
            size = stderr_file.tell()
            stderr_file.seek(max(0, size - 4000))
            stderr_tail = stderr_file.read().decode("utf-8", errors="replace").strip()
        if stderr_tail:
            log.debug("skill %s stderr: %s", name, stderr_tail)
        try:
            result = json.loads(proc.stdout.decode("utf-8")) if proc.stdout else None
        except ValueError:
            result = None
        if not isinstance(result, dict):
            message = f"Skill worker exited with code {proc.returncode} without a valid result"
            return {"ok": False, "error": message, "stderr": stderr_tail[-1500:]}
        return result
