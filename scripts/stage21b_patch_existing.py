# ruff: noqa: E501
"""Idempotent edits to earlier-stage files so ``LAB-nnn`` incident ids work (run once from the project root).

* every ``^INC-\\d{3}$`` schema pattern  -> ``^(INC|LAB)-\\d{3}$``;  sandbox/runner.py INCIDENT_RE likewise
* eval/harness.py: ``evaluate(..., pattern="INC-*.json")`` parameter (default unchanged)
* tools/__init__.py: ``LAB-`` ids resolve to ``data/lab/<id>`` (path-syntax checks stay in front)
* .gitignore: ``lab/out/``
A file that no longer matches is reported and left alone.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "incident_agent"


def swap(path: Path, old: str, new: str, done: str) -> None:
    if not path.exists():
        print(f"skip {path.relative_to(ROOT)}: not found")
        return
    text = path.read_text(encoding="utf-8")
    if done in text:
        return
    if old not in text:
        print(f"WARNING {path.relative_to(ROOT)}: expected text not found, left unchanged")
        return
    path.write_text(text.replace(old, new), encoding="utf-8", newline="")
    print(f"updated {path.relative_to(ROOT)}")


for py in sorted(SRC.rglob("*.py")):
    if "^INC-" in py.read_text(encoding="utf-8"):
        swap(py, r"^INC-\d{3}$", r"^(INC|LAB)-\d{3}$", "(INC|LAB)")
swap(
    SRC / "sandbox" / "runner.py",
    'INCIDENT_RE = re.compile(r"INC-\\d{3}")',
    'INCIDENT_RE = re.compile(r"(INC|LAB)-\\d{3}")',
    "(INC|LAB)",
)
h = SRC / "eval" / "harness.py"
swap(
    h,
    "    skip: dict[str, IncidentScore] | None = None,\n) -> dict:",
    '    skip: dict[str, IncidentScore] | None = None,\n    pattern: str = "INC-*.json",\n) -> dict:',
    'pattern: str = "INC-*.json"',
)
swap(h, '.glob("INC-*.json"))\n    if ids:', ".glob(pattern))\n    if ids:", ".glob(pattern))")
swap(
    SRC / "tools" / "__init__.py",
    '    return PROJECT_ROOT / "data" / "incidents" / incident_id\n',
    '    if incident_id.startswith("LAB-"):  # captured from the fault lab (real processes), kept apart from the dataset\n'
    '        return PROJECT_ROOT / "data" / "lab" / incident_id\n'
    '    return PROJECT_ROOT / "data" / "incidents" / incident_id\n',
    'incident_id.startswith("LAB-")',
)
gi = ROOT / ".gitignore"
if gi.exists() and "lab/out/" not in gi.read_text(encoding="utf-8"):
    gi.write_text(gi.read_text(encoding="utf-8").rstrip("\n") + "\nlab/out/\n", encoding="utf-8")
    print("updated .gitignore")
print("done")
