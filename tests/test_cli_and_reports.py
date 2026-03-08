from __future__ import annotations

from pathlib import Path
import json

from typer.testing import CliRunner

from pdf_toolkit.cli import app


runner = CliRunner()


def test_merge_writes_report(sample_pdf: Path, tmp_path: Path) -> None:
    output = tmp_path / "merged.pdf"
    report = tmp_path / "merge-report.json"
    result = runner.invoke(
        app,
        [
            "merge",
            str(sample_pdf),
            str(sample_pdf),
            "--output",
            str(output),
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["command"] == "merge"


def test_merge_respects_overwrite_exit_code(sample_pdf: Path, tmp_path: Path) -> None:
    output = tmp_path / "merged.pdf"
    output.write_bytes(b"already here")
    result = runner.invoke(
        app,
        [
            "merge",
            str(sample_pdf),
            str(sample_pdf),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 2


def test_redact_dry_run_report(sample_pdf: Path, tmp_path: Path) -> None:
    report = tmp_path / "redact-report.json"
    result = runner.invoke(
        app,
        [
            "redact",
            str(sample_pdf),
            "--pattern",
            "Hello",
            "--dry-run",
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["details"]["match_count"] >= 1


def test_doctor_returns_dependency_exit_code() -> None:
    result = runner.invoke(app, ["doctor", "--feature", "ocr"])
    assert result.exit_code in {0, 3}


def test_doctor_llm_returns_dependency_exit_code() -> None:
    result = runner.invoke(app, ["doctor", "--feature", "llm"])
    assert result.exit_code in {0, 3}


def test_extract_llm_writes_bundle_and_report(sample_pdf: Path, tmp_path: Path) -> None:
    output_dir = tmp_path / "llm"
    report = tmp_path / "extract-llm-report.json"
    result = runner.invoke(
        app,
        [
            "extract-llm",
            str(sample_pdf),
            "--output-dir",
            str(output_dir),
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["command"] == "extract-llm"
    assert any(path.suffix == ".jsonl" for path in output_dir.iterdir())


def test_analyze_llm_writes_outputs_and_report(sample_pdf: Path, tmp_path: Path, monkeypatch) -> None:
    output_dir = tmp_path / "analysis-root"
    report = tmp_path / "analyze-llm-report.json"

    def fake_invoke(*, model, schema, instructions, input_text):
        del model, instructions, input_text
        return schema(
            executive_summary="Summary",
            key_points=["Point"],
            risks=[],
            action_items=[],
            citations=[{"chunk_id": "sample-c001"}],
        )

    monkeypatch.setattr("pdf_toolkit.llm_analysis._invoke_structured_response", fake_invoke)
    result = runner.invoke(
        app,
        [
            "analyze-llm",
            str(sample_pdf),
            "--output-dir",
            str(output_dir),
            "--preset",
            "summary",
            "--report",
            str(report),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "success"
    assert payload["command"] == "analyze-llm"
    assert payload["details"]["preset"] == "summary"
    assert payload["details"]["bundle_path"].endswith("sample-llm.json")
    assert any(path.suffix == ".json" for path in (output_dir / "analysis").iterdir())
