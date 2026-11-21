import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "tools" / "replay" / "traceability_report.py"


def _run_report(tmp_path, payload, strict=True):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "report.json"
    input_path.write_text(json.dumps(payload), encoding="utf-8")
    cmd = [
        sys.executable,
        str(SCRIPT),
        "--input",
        str(input_path),
        "--output",
        str(output_path),
    ]
    if strict:
        cmd.append("--strict")
    result = subprocess.run(cmd, capture_output=True, text=True)
    report = None
    if output_path.exists():
        report = json.loads(output_path.read_text(encoding="utf-8"))
    return result, report


def test_empty_input_lacks_evidence(tmp_path):
    result, report = _run_report(tmp_path, [])
    assert report is not None
    assert report["status"] != "PASSED"
    assert result.returncode != 0


def test_zero_invariant_issue_lacks_evidence(tmp_path):
    payload = [{"issue": 1, "invariants": []}]
    result, report = _run_report(tmp_path, payload)
    assert report is not None
    assert report["status"] != "PASSED"
    assert result.returncode != 0


def test_uncovered_invariant_fails_strict(tmp_path):
    payload = [{"issue": 1, "invariants": [{"id": "inv-1", "covered": False}]}]
    result, report = _run_report(tmp_path, payload)
    assert report is not None
    assert report["status"] != "PASSED"
    assert result.returncode != 0


def test_covered_invariant_passes_strict(tmp_path):
    payload = [{"issue": 1, "invariants": [{"id": "inv-1", "covered": True}]}]
    result, report = _run_report(tmp_path, payload)
    assert report is not None
    assert report["status"] == "PASSED"
    assert result.returncode == 0
