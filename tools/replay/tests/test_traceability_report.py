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


def test_nonexistent_test_id_described_as_reference(tmp_path):
    # Failure case: a referenced test ID that was never executed must be
    # described as a reference/link, not as a successful execution.
    payload = [
        {
            "issue": 1,
            "invariants": [
                {"id": "inv-1", "covered": True, "test_ids": ["does-not-exist-9999"]}
            ],
        }
    ]
    result, report = _run_report(tmp_path, payload)
    assert report is not None

    combined = (result.stdout + result.stderr).lower()
    # Human output must not claim execution or passing tests.
    assert "executed" not in combined
    assert "verified" not in combined
    assert "passed" not in combined
    # Human output should use reference/link terminology.
    assert "referenc" in combined or "linked" in combined

    # JSON and human output must describe the same evidence level.
    serialized = json.dumps(report).lower()
    assert "executed" not in serialized
    assert "verified" not in serialized
    assert "passed" not in serialized


def test_referenced_test_id_success_case(tmp_path):
    # Success case: a referenced test ID is reported as a reference and the
    # coverage metric is documented, without claiming execution.
    payload = [
        {
            "issue": 1,
            "invariants": [
                {"id": "inv-1", "covered": True, "test_ids": ["test_alpha"]}
            ],
        }
    ]
    result, report = _run_report(tmp_path, payload)
    assert report is not None
    assert report["status"] == "PASSED"
    assert result.returncode == 0

    combined = (result.stdout + result.stderr).lower()
    assert "executed" not in combined
    assert "verified" not in combined
    assert "passed" not in combined
    assert "referenc" in combined or "linked" in combined
    # Coverage metric meaning should be documented in the output.
    assert "coverage" in combined
