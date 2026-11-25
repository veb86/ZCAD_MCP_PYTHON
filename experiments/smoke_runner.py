"""Smoke test: generate a DXF and run bundled skills through SkillRunner."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ezdxf
from config import load_settings
from skill_runner import SkillRunner
import dataclasses

tmp = Path(tempfile.mkdtemp())
s = dataclasses.replace(load_settings({}, load_dotenv=False), workspace_dir=tmp)
doc = ezdxf.new()
msp = doc.modelspace()
for i in range(1000):
    msp.add_line((0, i * 10), (5000, i * 10), dxfattribs={"layer": "WALLS"})
for i in range(300):
    msp.add_line((i * 10, 0), (i * 10, 3000))
msp.add_lwpolyline([(0, 0), (100, 0), (100, 100)], close=True)
dxf = tmp / "selection.dxf"
doc.saveas(dxf)
r = SkillRunner(s)
print(json.dumps(r.list_skills(), indent=1)[:1500])
for name in ("extract_line_stats", "find_parallel_lines"):
    res = r.run(name, dxf, {}, max_segments=5)
    print(name, json.dumps(res)[:1500])
