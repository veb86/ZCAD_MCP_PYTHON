"""ZCAD MCP server: skill orchestrator + channel to ZCAD (stdio transport).

Run: ``python server.py`` (an MCP client starts it as a subprocess and talks
JSON-RPC over stdin/stdout). Logs go to stderr only.

The model never receives raw line arrays of a drawing: ZCAD exports a DXF into
``workspace/``, skills (``skills/*.py``) analyse it in a separate process and
return compact JSON (stats + at most ``max_segments`` candidates), and the
model sends back only the final geometry with ``zcad_draw_lines`` /
``zcad_draw_polylines``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from mcp.server.mcpserver import MCPServer

from config import PROJECT_DIR, Settings, load_settings, setup_logging
from dxf_workspace import Workspace, WorkspaceError, read_header_extents
from skill_runner import SkillError, SkillRunner
from zcad_client import ZcadBridgeError, ZcadClient

log = logging.getLogger("zcad_mcp.server")

SERVER_NAME = "zcad-mcp"

INSTRUCTIONS = """\
Tools to analyse ZCAD drawings with Python "skills" and to draw results back.

Typical flow ("walls from selection"):
1. zcad_status - check that the ZCAD Bridge (HTTPStart in ZCAD) is reachable.
2. zcad_export_selection - ZCAD writes a DXF to the workspace; it becomes the current DXF.
3. list_skills / run_skill(name, params_json) - analyse the current DXF. Results are compact:
   stats + at most max_segments candidates. Never ask for every line of the drawing.
4. If no skill fits: get_skill_template, then create_skill(name, source_code) and run_skill
   immediately; improve it with update_skill (or create_skill(..., overwrite=true)).
5. zcad_draw_lines / zcad_draw_polylines - draw the final result into ZCAD.
Every tool answers a JSON object with "ok"; on failure "ok" is false and "error" explains why.
"""


def _parse_json_arg(value: Any, name: str, expected: type | tuple[type, ...]) -> Any:
    """Tools take JSON strings (``params_json``...), but some clients send objects."""
    if isinstance(value, str):
        text = value.strip() or ("{}" if expected is dict else "[]")
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"{name} is not valid JSON: {exc}") from None
    if not isinstance(value, expected):
        kind = "object" if expected is dict else "array"
        raise ValueError(f"{name} must be a JSON {kind}")
    return value


def _error(exc: BaseException) -> dict[str, Any]:
    if isinstance(exc, ZcadBridgeError):
        return exc.to_dict()
    if isinstance(exc, (ValueError, SkillError, WorkspaceError)):
        return {"ok": False, "error": str(exc)}
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _call(tool: str, fn: Callable[[], dict[str, Any]], **log_args: Any) -> dict[str, Any]:
    started = time.monotonic()
    log.info("tool %s %s", tool, log_args or "")
    try:
        result = fn()
    except Exception as exc:  # noqa: BLE001 - never crash the server because of a tool
        log.warning("tool %s failed: %s", tool, exc, exc_info=not isinstance(exc, (ValueError, ZcadBridgeError)))
        result = _error(exc)
    log.info("tool %s done ok=%s in %.3fs", tool, result.get("ok"), time.monotonic() - started)
    return result


async def _acall(tool: str, fn: Callable[[], Awaitable[dict[str, Any]]], **log_args: Any) -> dict[str, Any]:
    started = time.monotonic()
    log.info("tool %s %s", tool, log_args or "")
    try:
        result = await fn()
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ZcadBridgeError):
            log.error("ZCAD Bridge error in %s: [%s] %s", tool, exc.code, exc.message)
        else:
            log.warning("tool %s failed: %s", tool, exc, exc_info=not isinstance(exc, ValueError))
        result = _error(exc)
    log.info("tool %s done ok=%s in %.3fs", tool, result.get("ok"), time.monotonic() - started)
    return result


def build_server(
    settings: Settings | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> MCPServer:
    """Create the MCP server. ``transport`` lets tests replace the ZCAD Bridge with a mock."""
    settings = settings or load_settings()
    settings.ensure_dirs()
    workspace = Workspace(settings)
    runner = SkillRunner(settings)
    zcad = ZcadClient(settings, transport=transport)

    server = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS)
    # Expose internals for tests / embedding.
    server.zcad_settings = settings  # type: ignore[attr-defined]
    server.zcad_workspace = workspace  # type: ignore[attr-defined]
    server.zcad_runner = runner  # type: ignore[attr-defined]
    server.zcad_client = zcad  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ skills
    @server.tool()
    def list_skills() -> list[dict[str, Any]]:
        """List available DXF analysis skills: [{name, description, params_schema}].

        params_schema is a JSON schema of the params accepted by run_skill(params_json=...).
        """
        answer = _call("list_skills", lambda: {"ok": True, "skills": runner.list_skills()})
        return answer["skills"] if answer.get("ok") else [answer]

    @server.tool()
    def get_skill_source(name: str) -> dict[str, Any]:
        """Return the Python source of a skill: {name, description, source}."""
        return _call("get_skill_source", lambda: {"ok": True, **runner.get_source(name)}, name=name)

    @server.tool()
    def get_skill_template() -> dict[str, Any]:
        """Return the skill contract and a minimal working example to base new skills on."""

        def do() -> dict[str, Any]:
            source = (PROJECT_DIR / "skill_template.py").read_text(encoding="utf-8")
            return {"ok": True, "name": "skill_template", "source": source}

        return _call("get_skill_template", do)

    @server.tool()
    def create_skill(name: str, source_code: str, description: str = "", overwrite: bool = False) -> dict[str, Any]:
        """Save a new skill to skills/<name>.py (validated statically, never executed here).

        source_code must define SKILL = {"name", "description", "params"} and
        run(dxf_path: str, params: dict) -> dict returning compact JSON
        {ok, skill, stats, result, warnings} with at most params["max_segments"] candidates
        (see get_skill_template). Set overwrite=true to replace an existing skill.
        Answer: {ok, name, path} or {ok: false, error, errors}.
        """
        return _call(
            "create_skill",
            lambda: runner.save(name, source_code, description, overwrite),
            name=name,
            overwrite=overwrite,
            source_bytes=len(source_code or ""),
        )

    @server.tool()
    def update_skill(name: str, source_code: str, description: str = "") -> dict[str, Any]:
        """Replace the source of an existing skill (same as create_skill with overwrite=true)."""

        def do() -> dict[str, Any]:
            if not runner.skill_path(name).is_file():
                return {"ok": False, "name": name, "error": f"Skill {name!r} not found; use create_skill"}
            return runner.save(name, source_code, description, overwrite=True)

        return _call("update_skill", do, name=name, source_bytes=len(source_code or ""))

    @server.tool()
    def delete_skill(name: str) -> dict[str, Any]:
        """Delete skills/<name>.py (and its metadata)."""
        return _call("delete_skill", lambda: runner.delete(name), name=name)

    @server.tool()
    def run_skill(
        name: str,
        params_json: str = "{}",
        dxf_path: str | None = None,
        max_segments: int | None = None,
    ) -> dict[str, Any]:
        """Run a skill on a DXF (default: the current DXF, see get_current_dxf).

        params_json is a JSON object with the skill params (see list_skills params_schema).
        max_segments limits every list in the answer (default ZCAD_MCP_MAX_SEGMENTS).
        Answer: {ok, skill, stats, result, warnings, elapsed_sec} or {ok: false, error}.
        """

        def do() -> dict[str, Any]:
            params = _parse_json_arg(params_json, "params_json", dict)
            if dxf_path:
                path = workspace.resolve_dxf_path(dxf_path, must_exist=True)
            else:
                path = workspace.current_dxf()
                if not path.is_file():
                    return {
                        "ok": False,
                        "skill": name,
                        "error": f"Current DXF {path} does not exist. Call zcad_export_selection "
                        "or set_current_dxf, or pass dxf_path.",
                    }
            return runner.run(name, path, params, max_segments)

        return _call("run_skill", do, name=name, dxf_path=dxf_path, max_segments=max_segments)

    # ------------------------------------------------------------------ current DXF
    @server.tool()
    def get_current_dxf() -> dict[str, Any]:
        """Return the DXF used by run_skill by default: {path, exists, size, mtime}."""
        return _call("get_current_dxf", lambda: {"ok": True, **workspace.describe()})

    @server.tool()
    def set_current_dxf(path: str) -> dict[str, Any]:
        """Make an existing .dxf file the current DXF (relative paths are inside the workspace)."""

        def do() -> dict[str, Any]:
            resolved = workspace.set_current_dxf(path)
            return {"ok": True, **workspace.describe(resolved)}

        return _call("set_current_dxf", do, path=path)

    # ------------------------------------------------------------------ ZCAD
    @server.tool()
    async def zcad_status() -> dict[str, Any]:
        """Check that the ZCAD Bridge is reachable: {ok, status, detail?}."""
        return await _acall("zcad_status", zcad.status)

    @server.tool()
    async def zcad_export_selection(out_path: str | None = None) -> dict[str, Any]:
        """Ask ZCAD to export the drawing fragment to DXF and make it the current DXF.

        out_path: target .dxf (default workspace/selection.dxf; relative = inside the workspace).
        Answer: {ok, path, bbox?, size}. Then call run_skill without dxf_path.
        """

        async def do() -> dict[str, Any]:
            target = (
                workspace.resolve_dxf_path(out_path, must_exist=False) if out_path else settings.default_dxf_path.resolve()
            )
            exported = await zcad.export_dxf(target)
            workspace.set_current_dxf(target)
            answer: dict[str, Any] = {"ok": True, "path": str(target), "size": target.stat().st_size}
            bbox = read_header_extents(target)
            if bbox:
                answer["bbox"] = bbox
            if exported.get("bridge_result"):
                answer["detail"] = exported["bridge_result"]
            return answer

        return await _acall("zcad_export_selection", do, out_path=out_path)

    @server.tool()
    async def zcad_draw_lines(lines_json: str, layer: str = "IM_ANALYSIS", color: int = 1) -> dict[str, Any]:
        """Draw line segments in ZCAD.

        lines_json: JSON array [{"x1":..,"y1":..,"x2":..,"y2":..}, ...] (e.g. skill candidates).
        Answer: {ok, drawn_count}.
        """

        async def do() -> dict[str, Any]:
            lines = _parse_json_arg(lines_json, "lines_json", list)
            return await zcad.draw_lines(lines, layer, color)

        return await _acall("zcad_draw_lines", do, layer=layer, color=color, json_chars=len(str(lines_json)))

    @server.tool()
    async def zcad_draw_polylines(polylines_json: str, layer: str = "IM_WALLS", color: int = 1) -> dict[str, Any]:
        """Draw polylines in ZCAD.

        polylines_json: JSON array [{"points": [[x, y], ...], "closed": false}, ...].
        Answer: {ok, drawn_count} (drawn_count = number of polylines).
        """

        async def do() -> dict[str, Any]:
            polylines = _parse_json_arg(polylines_json, "polylines_json", list)
            return await zcad.draw_polylines(polylines, layer, color)

        return await _acall("zcad_draw_polylines", do, layer=layer, color=color, json_chars=len(str(polylines_json)))

    return server


def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    settings.ensure_dirs()
    log.info(
        "starting %s (stdio): bridge=%s workspace=%s skills=%s max_segments=%s timeout=%ss",
        SERVER_NAME,
        settings.bridge_url,
        settings.workspace_dir,
        settings.skills_dir,
        settings.max_segments,
        settings.skill_timeout_sec,
    )
    build_server(settings).run("stdio")


if __name__ == "__main__":
    main()
