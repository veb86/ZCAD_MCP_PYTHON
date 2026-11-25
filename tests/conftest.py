"""Shared fixtures: isolated workspace/skills dirs, a fixture DXF and a mock ZCAD Bridge."""

from __future__ import annotations

import dataclasses
import shutil
import sys
from pathlib import Path

import ezdxf
import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "examples"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from config import load_settings  # noqa: E402
from mock_zcad_bridge import MockZcadBridge, write_sample_plan  # noqa: E402

BUNDLED_SKILLS = ("extract_line_stats", "find_parallel_lines")


@pytest.fixture
def settings(tmp_path: Path):
    """Settings with temporary workspace/skills dirs holding a copy of the bundled skills."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    for name in BUNDLED_SKILLS:
        shutil.copy(ROOT / "skills" / f"{name}.py", skills_dir / f"{name}.py")
    base = load_settings({}, load_dotenv=False)
    return dataclasses.replace(
        base,
        workspace_dir=tmp_path / "workspace",
        skills_dir=skills_dir,
        skill_timeout_sec=30.0,
        export_wait_sec=2.0,
        bridge_url="http://zcad.test",
    )


@pytest.fixture
def grid_dxf(settings) -> Path:
    """300 horizontal + 200 vertical LINEs, one closed LWPOLYLINE and a circle."""
    settings.ensure_dirs()
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    for i in range(300):
        msp.add_line((0, i * 10), (5000, i * 10), dxfattribs={"layer": "H"})
    for i in range(200):
        msp.add_line((i * 10, 0), (i * 10, 3000), dxfattribs={"layer": "V"})
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 100)], close=True, dxfattribs={"layer": "P"})
    msp.add_circle((50, 50), 10)
    path = settings.workspace_dir / "grid.dxf"
    doc.saveas(path)
    return path


@pytest.fixture
def plan_dxf(settings) -> Path:
    settings.ensure_dirs()
    path = settings.workspace_dir / "plan.dxf"
    write_sample_plan(str(path))
    return path


@pytest.fixture
def bridge() -> MockZcadBridge:
    return MockZcadBridge()


@pytest.fixture
def detect_walls_source() -> str:
    return (ROOT / "examples" / "detect_walls_skill.py").read_text(encoding="utf-8")
