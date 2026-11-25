"""Environment / .env configuration."""

from __future__ import annotations

import pytest

from config import PROJECT_DIR, ConfigError, load_settings


def test_defaults():
    s = load_settings({}, load_dotenv=False)
    assert s.bridge_url == "http://127.0.0.1:5000"
    assert s.workspace_dir == PROJECT_DIR / "workspace" and s.skills_dir == PROJECT_DIR / "skills"
    assert s.max_segments == 200 and s.skill_timeout_sec == 60.0
    assert s.default_dxf_path == PROJECT_DIR / "workspace" / "selection.dxf"


def test_env_values(tmp_path):
    s = load_settings(
        {
            "ZCAD_BRIDGE_URL": "http://10.0.0.5:5000/",
            "ZCAD_MCP_WORKSPACE": str(tmp_path / "ws"),
            "ZCAD_MCP_SKILLS": "custom_skills",
            "ZCAD_MCP_MAX_SEGMENTS": "50",
            "ZCAD_MCP_SKILL_TIMEOUT_SEC": "2.5",
            "ZCAD_DRAW_MODE": "LINE",
        },
        load_dotenv=False,
    )
    assert s.bridge_url == "http://10.0.0.5:5000"
    assert s.workspace_dir == tmp_path / "ws"
    assert s.skills_dir == PROJECT_DIR / "custom_skills"  # relative to the project, not cwd
    assert (s.max_segments, s.skill_timeout_sec, s.draw_mode) == (50, 2.5, "line")
    with pytest.raises(Exception):
        s.max_segments = 1  # type: ignore[misc]


def test_dotenv_file_does_not_override_environment(tmp_path):
    env_file = tmp_path / "test.env"
    env_file.write_text("ZCAD_MCP_MAX_SEGMENTS=11\nZCAD_BRIDGE_URL=http://from-file:1\n", encoding="utf-8")
    s = load_settings({"ZCAD_MCP_ENV_FILE": str(env_file), "ZCAD_BRIDGE_URL": "http://from-env:2"})
    assert s.max_segments == 11 and s.bridge_url == "http://from-env:2"


@pytest.mark.parametrize(
    "env",
    [
        {"ZCAD_BRIDGE_URL": "ftp://x"},
        {"ZCAD_MCP_MAX_SEGMENTS": "0"},
        {"ZCAD_MCP_MAX_SEGMENTS": "many"},
        {"ZCAD_MCP_SKILL_TIMEOUT_SEC": "-1"},
        {"ZCAD_DRAW_MODE": "spline"},
    ],
)
def test_invalid_values(env):
    with pytest.raises(ConfigError):
        load_settings(env, load_dotenv=False)


def test_ensure_dirs_creates_workspace_and_skills(tmp_path):
    s = load_settings({"ZCAD_MCP_WORKSPACE": str(tmp_path / "w"), "ZCAD_MCP_SKILLS": str(tmp_path / "s")}, load_dotenv=False)
    s.ensure_dirs()
    assert (tmp_path / "w").is_dir() and (tmp_path / "s").is_dir()
