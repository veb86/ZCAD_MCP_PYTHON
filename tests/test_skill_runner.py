"""SkillRunner: validation, listing, execution on a fixture DXF, truncation and guards."""

from __future__ import annotations

import dataclasses
import json

import pytest

from skill_runner import SkillError, SkillRunner, check_source, compact_result, validate_name

GOOD_SKILL = '''
SKILL = {"name": "count_lines", "description": "Counts LINE entities.", "params": {"layers": [], "limit": 5}}

import ezdxf

def run(dxf_path, params):
    doc = ezdxf.readfile(dxf_path)
    lines = doc.modelspace().query("LINE")
    return {"ok": True, "stats": {"lines": len(lines)}, "result": {"max_segments": params["max_segments"]}}
'''


def skill(body: str, name: str = "probe") -> str:
    return f'SKILL = {{"name": "{name}", "description": "probe", "params": {{}}}}\n' + body


@pytest.fixture
def runner(settings):
    settings.ensure_dirs()
    return SkillRunner(settings)


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("name", ["walls", "_x", "Find_Walls2"])
def test_valid_names(name):
    assert validate_name(name) == name


@pytest.mark.parametrize("name", ["", "1abc", "a-b", "../evil", "a.b", "__init__", "x" * 65, "имя"])
def test_invalid_names(name):
    with pytest.raises(SkillError):
        validate_name(name)


@pytest.mark.parametrize(
    "source, fragment",
    [
        ("import socket\ndef run(d, p): return {}", "socket"),
        ("from subprocess import run as r\ndef run(d, p): return {}", "subprocess"),
        ("import os\ndef run(d, p): os.system('ls'); return {}", "os.system"),
        ("from os import popen\ndef run(d, p): return {}", "process-spawning"),
        ("def run(d, p): return eval('1')", "eval"),
        ("import urllib.request\ndef run(d, p): return {}", "urllib"),
        ("def run(d, p): return ().__class__.__bases__", "__bases__"),
        ("from . import x\ndef run(d, p): return {}", "relative"),
        ("def other(): pass", "run(dxf_path, params)"),
        ("def run(d, p) return 1", "SyntaxError"),
        ("async def run(d, p): return {}", "async"),
        ('SKILL = {"name": "other"}\ndef run(d, p): return {}', "file name"),
    ],
)
def test_check_source_rejects(source, fragment):
    errors = check_source(source, "probe").errors
    assert errors and any(fragment in e for e in errors), errors


def test_check_source_accepts_good_skill():
    check = check_source(GOOD_SKILL, "count_lines")
    assert check.errors == []
    assert check.skill_meta["params"] == {"layers": [], "limit": 5}


# ------------------------------------------------------------------ listing / CRUD
def test_list_bundled_skills(runner):
    skills = {s["name"]: s for s in runner.list_skills()}
    assert set(skills) == {"extract_line_stats", "find_parallel_lines"}
    for info in skills.values():
        assert info["description"]
        assert info["params_schema"]["type"] == "object"
        assert "errors" not in info
    props = skills["find_parallel_lines"]["params_schema"]["properties"]
    assert props["angle_tol_deg"] == {"default": 1.0, "type": "number"}
    assert props["layers"]["type"] == "array"


def test_create_run_delete(runner, grid_dxf):
    saved = runner.save("count_lines", GOOD_SKILL, description="My description")
    assert saved["ok"] and saved["path"].endswith("count_lines.py")
    assert runner.describe("count_lines")["description"] == "My description"
    assert runner.save("count_lines", GOOD_SKILL)["ok"] is False  # no silent overwrite
    assert runner.save("count_lines", GOOD_SKILL, overwrite=True)["overwritten"] is True

    result = runner.run("count_lines", grid_dxf, {}, max_segments=7)
    assert result["ok"] is True, result
    assert result["stats"] == {"lines": 500}
    assert result["result"] == {"max_segments": 7}
    assert result["skill"] == "count_lines" and result["elapsed_sec"] > 0

    assert runner.delete("count_lines")["ok"] is True
    assert "count_lines" not in {s["name"] for s in runner.list_skills()}
    assert runner.run("count_lines", grid_dxf)["ok"] is False


def test_save_rejects_invalid_source(runner):
    answer = runner.save("bad", "import socket\ndef run(d, p): return {}")
    assert answer["ok"] is False and answer["errors"]
    assert not runner.skill_path("bad").exists()


# ------------------------------------------------------------------ bundled skills
def test_extract_line_stats(runner, grid_dxf):
    result = runner.run("extract_line_stats", grid_dxf, {})
    assert result["ok"] is True, result
    assert result["stats"]["segments_used"] == 503  # 500 lines + 3 polyline segments
    assert result["result"]["orientation"] == {"horizontal": 301, "vertical": 201, "other": 1}
    assert result["result"]["bbox"] == {"min_x": 0.0, "min_y": 0.0, "max_x": 5000.0, "max_y": 3000.0}
    assert result["result"]["entity_types"]["CIRCLE"] == 1
    assert len(json.dumps(result)) < 5000  # aggregates only, no geometry


def test_find_parallel_lines_respects_max_segments(runner, grid_dxf):
    result = runner.run("find_parallel_lines", grid_dxf, {"angle_tol_deg": 0.5}, max_segments=10)
    assert result["ok"] is True, result
    assert result["stats"]["lines_used"] == 503
    assert len(result["result"]["candidates"]) == 10
    angles = sorted(round(g["angle_deg"]) for g in result["result"]["groups"])
    assert angles == [0, 45, 90]
    assert any("max_segments" in w for w in result["warnings"])


def test_max_segments_from_params_and_default(runner, grid_dxf, settings):
    result = runner.run("find_parallel_lines", grid_dxf, {"max_segments": 3})
    assert len(result["result"]["candidates"]) == 3
    small = SkillRunner(dataclasses.replace(settings, max_segments=4))
    assert len(small.run("find_parallel_lines", grid_dxf, {})["result"]["candidates"]) == 4
    assert runner.run("find_parallel_lines", grid_dxf, {}, max_segments=0)["ok"] is False


# ------------------------------------------------------------------ truncation
def test_compact_result_truncates_lists():
    raw = {"stats": {"n": 1000}, "result": {"lines": list(range(1000)), "nested": [{"pts": list(range(50))}]}}
    out = compact_result(raw, "s", max_items=10, max_bytes=100_000)
    assert out["ok"] is True and out["skill"] == "s"
    assert out["result"]["lines"] == list(range(10))
    assert out["result"]["nested"][0]["pts"] == list(range(10))
    assert out["truncated"] == {"result.lines": 1000, "result.nested[].pts": 50}
    assert len(out["warnings"]) == 2


def test_compact_result_size_limit():
    raw = {"stats": {"n": 1}, "result": {"blob": "x" * 5000}}
    out = compact_result(raw, "s", max_items=10, max_bytes=1024)
    assert out["ok"] is False and "too large" in out["error"]
    assert out["stats"] == {"n": 1} and "result" not in out


def test_runner_truncates_greedy_skill(runner, grid_dxf):
    greedy = skill(
        "import ezdxf\n"
        "def run(dxf_path, params):\n"
        "    msp = ezdxf.readfile(dxf_path).modelspace()\n"
        "    lines = [[l.dxf.start.x, l.dxf.start.y, l.dxf.end.x, l.dxf.end.y] for l in msp.query('LINE')]\n"
        "    return {'stats': {'n': len(lines)}, 'result': {'lines': lines}}\n",
        name="greedy",
    )
    assert runner.save("greedy", greedy)["ok"]
    result = runner.run("greedy", grid_dxf, {}, max_segments=25)
    assert result["ok"] is True
    assert len(result["result"]["lines"]) == 25
    assert result["truncated"] == {"result.lines": 500}
    assert result["stats"] == {"n": 500}


# ------------------------------------------------------------------ errors & guards
def run_probe(runner, dxf, body, **kw):
    assert runner.save("probe", skill(body), overwrite=True)["ok"], check_source(skill(body)).errors
    return runner.run("probe", dxf, {}, **kw)


def test_skill_exception_is_reported(runner, grid_dxf):
    result = run_probe(runner, grid_dxf, "def run(d, p):\n    raise ValueError('bad input')\n")
    assert result["ok"] is False
    assert "bad input" in result["error"] and result["error_type"] == "ValueError"


def test_skill_must_return_dict(runner, grid_dxf):
    result = run_probe(runner, grid_dxf, "def run(d, p):\n    return [1, 2]\n")
    assert result["ok"] is False and "must return a dict" in result["error"]


def test_print_does_not_break_protocol(runner, grid_dxf):
    result = run_probe(runner, grid_dxf, "def run(d, p):\n    print('noise' * 100)\n    return {'stats': {}}\n")
    assert result["ok"] is True, result


def test_timeout_kills_skill(settings, grid_dxf):
    runner = SkillRunner(dataclasses.replace(settings, skill_timeout_sec=1.5))
    result = run_probe(runner, grid_dxf, "def run(d, p):\n    while True:\n        pass\n")
    assert result["ok"] is False and "timed out" in result["error"]
    assert result["elapsed_sec"] < 15


def test_guard_blocks_subprocess_at_runtime(runner, grid_dxf):
    # getattr() hides os.system from the static check; the audit hook must stop it.
    body = "import os\ndef run(d, p):\n    getattr(os, 'sys' + 'tem')('echo pwned')\n    return {}\n"
    result = run_probe(runner, grid_dxf, body)
    assert result["ok"] is False and "os.system" in result["error"]


def test_guard_blocks_network_at_runtime(runner, grid_dxf):
    # pkgutil.resolve_name() hides the import from the static check.
    body = (
        "import pkgutil\n"
        "def run(d, p):\n"
        "    sock = pkgutil.resolve_name('soc' + 'ket')\n"
        "    sock.create_connection(('127.0.0.1', 9), timeout=1)\n"
        "    return {}\n"
    )
    result = run_probe(runner, grid_dxf, body)
    # Blocked either on the socket import itself or on its dependency (selectors) - both fine.
    assert result["ok"] is False and "not allowed" in result["error"], result


def test_guard_blocks_socket_even_if_preloaded(runner, grid_dxf):
    body = (
        "import sys\n"
        "def run(d, p):\n"
        "    mod = sys.modules.get('so' + 'cket')\n"
        "    if mod is None:\n"
        "        return {'stats': {'preloaded': False}}\n"
        "    mod.socket()\n"
        "    return {'stats': {'preloaded': True}}\n"
    )
    result = run_probe(runner, grid_dxf, body)
    if result.get("ok"):
        assert result["stats"] == {"preloaded": False}
    else:
        assert "socket" in result["error"] and "not allowed" in result["error"]


def test_guard_blocks_dynamic_forbidden_import(runner, grid_dxf):
    body = "import pkgutil\ndef run(d, p):\n    pkgutil.resolve_name('subpro' + 'cess')\n    return {}\n"
    result = run_probe(runner, grid_dxf, body)
    assert result["ok"] is False and "not allowed" in result["error"], result


def test_guard_blocks_write_outside_workspace(runner, grid_dxf, tmp_path):
    target = tmp_path / "outside.txt"
    body = f"def run(d, p):\n    open({str(target)!r}, 'w').write('x')\n    return {{}}\n"
    result = run_probe(runner, grid_dxf, body)
    assert result["ok"] is False and "outside workspace" in result["error"]
    assert not target.exists()


def test_guard_allows_write_inside_workspace(runner, grid_dxf, settings):
    target = settings.workspace_dir / "skill_output.txt"
    body = f"def run(d, p):\n    open({str(target)!r}, 'w').write('x')\n    return {{'stats': {{}}}}\n"
    assert run_probe(runner, grid_dxf, body)["ok"] is True
    assert target.read_text() == "x"


def test_guard_blocks_reading_foreign_files(runner, grid_dxf, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    body = f"def run(d, p):\n    return {{'stats': {{'s': open({str(secret)!r}).read()}}}}\n"
    result = run_probe(runner, grid_dxf, body)
    assert result["ok"] is False and "reading" in result["error"]


def test_worker_env_has_no_zcad_variables(runner, grid_dxf, monkeypatch):
    monkeypatch.setenv("ZCAD_BRIDGE_TOKEN", "secret-token")
    body = "import os\ndef run(d, p):\n    return {'stats': {'keys': sorted(k for k in os.environ if k.startswith('ZCAD'))}}\n"
    assert run_probe(runner, grid_dxf, body)["stats"] == {"keys": []}
