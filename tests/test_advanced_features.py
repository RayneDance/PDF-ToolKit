from __future__ import annotations

from pathlib import Path
import json
import shutil

import fitz
import yaml

from pdf_toolkit.batch import build_file_batch_manifest, build_folder_batch_manifest, process_watch_folder_once, run_batch, write_manifest
from pdf_toolkit.config import load_config
from pdf_toolkit.duplicates import remove_duplicate_pdfs, scan_duplicate_pdfs
from pdf_toolkit.llm_analysis import analyze_pdf_with_llm
from pdf_toolkit.llm_extract import extract_for_llm
from pdf_toolkit.ocr import run_ocr, scan_detect
from pdf_toolkit.redaction import parse_redaction_box, run_redaction
from pdf_toolkit.tables import extract_tables_to_files


def test_load_config_reads_project_file(tmp_path: Path, monkeypatch) -> None:
    config_file = tmp_path / "pdf-toolkit.toml"
    config_file.write_text(
        """
[toolkit]
default_output_root = "output/root"
report_format = "json"
ocr_language = "spa"
temp_dir = "tmp/work"
overwrite = true
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    config = load_config()
    assert config.default_output_root == Path("output/root")
    assert config.ocr_language == "spa"
    assert config.temp_dir == Path("tmp/work")
    assert config.overwrite is True


def test_scan_detect_classifies_documents(sample_pdf: Path, scanned_image_pdf: Path, mixed_pdf: Path) -> None:
    assert scan_detect(sample_pdf)["summary"] == "text-based"
    assert scan_detect(scanned_image_pdf)["summary"] == "image-based"
    assert scan_detect(mixed_pdf)["summary"] == "mixed"


def test_parse_redaction_box() -> None:
    box = parse_redaction_box("2,10,20,30,40")
    assert box.page_number == 2
    assert box.x1 == 10
    assert box.y2 == 40


def test_redaction_removes_text(sample_pdf: Path, tmp_path: Path) -> None:
    output = tmp_path / "redacted.pdf"
    result = run_redaction(
        sample_pdf,
        output_path=output,
        patterns=["Hello"],
        regex=False,
        case_sensitive=False,
        page_spec=None,
        box_specs=[],
        label=None,
        dry_run=False,
    )
    assert output in result["outputs"]
    text = fitz.open(output)[0].get_text()
    assert "Hello" not in text


def test_redaction_dry_run_reports_matches(sample_pdf: Path) -> None:
    result = run_redaction(
        sample_pdf,
        output_path=None,
        patterns=["Hello"],
        regex=False,
        case_sensitive=False,
        page_spec=None,
        box_specs=[],
        label=None,
        dry_run=True,
    )
    assert result["details"]["match_count"] >= 1


def test_run_ocr_missing_dependency(sample_pdf: Path, tmp_path: Path, monkeypatch) -> None:
    from pdf_toolkit.errors import DependencyMissingError

    monkeypatch.setattr("pdf_toolkit.ocr.ensure_command_available", lambda *args, **kwargs: (_ for _ in ()).throw(DependencyMissingError("ocr missing")))
    try:
        run_ocr(
            sample_pdf,
            tmp_path / "ocr.pdf",
            language="eng",
            skip_existing_text=True,
            text_output=None,
            json_output=None,
            force=False,
            temp_dir=tmp_path,
        )
    except DependencyMissingError as exc:
        assert "ocr missing" in str(exc)
    else:
        raise AssertionError("Expected DependencyMissingError")


def test_run_ocr_success_with_mocked_runner(sample_pdf: Path, tmp_path: Path, monkeypatch) -> None:
    def fake_run(command, check, capture_output, text, env):
        shutil.copy2(sample_pdf, tmp_path / "ocr.pdf")
        class Result:
            returncode = 0
        return Result()

    monkeypatch.setattr(
        "pdf_toolkit.ocr.ensure_command_available",
        lambda name, *args, **kwargs: {
            "ocrmypdf": "ocrmypdf.exe",
            "tesseract": "tesseract.exe",
            "gswin64c": "gswin64c.exe",
        }[name],
    )
    monkeypatch.setattr("pdf_toolkit.ocr.subprocess.run", fake_run)

    text_output = tmp_path / "ocr.txt"
    json_output = tmp_path / "ocr.json"
    result = run_ocr(
        sample_pdf,
        tmp_path / "ocr.pdf",
        language="eng",
        skip_existing_text=False,
        text_output=text_output,
        json_output=json_output,
        force=True,
        temp_dir=tmp_path,
    )
    assert text_output in result["outputs"]
    assert json_output in result["outputs"]
    assert json_output.exists()


def test_extract_tables_to_files(table_pdf: Path, tmp_path: Path) -> None:
    result = extract_tables_to_files(
        table_pdf,
        tmp_path / "tables",
        page_spec=None,
        format_name="all",
        ocr_first=False,
        ocr_language="eng",
        temp_dir=tmp_path,
    )
    outputs = [Path(path) if isinstance(path, str) else path for path in result["outputs"]]
    assert any(path.suffix == ".csv" for path in outputs)
    assert any(path.suffix == ".xlsx" for path in outputs)
    assert any(path.suffix == ".json" for path in outputs)


def test_extract_for_llm_writes_markdown_json_and_jsonl(sample_pdf: Path, tmp_path: Path) -> None:
    result = extract_for_llm(sample_pdf, tmp_path / "llm")
    outputs = [Path(path) if isinstance(path, str) else path for path in result["outputs"]]
    assert {path.suffix for path in outputs} == {".md", ".json", ".jsonl"}

    json_path = next(path for path in outputs if path.suffix == ".json")
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["format"] == "pdf-toolkit-llm-bundle-v1"
    assert payload["document"]["page_count"] == 3
    assert payload["document"]["source_sha256"]
    assert payload["document"]["extraction_quality"]["ocr_recommended"] is False
    assert len(payload["pages"]) == 3
    assert payload["sections"]
    assert payload["chunks"]
    assert payload["chunks"][0]["heading"]
    assert payload["chunks"][0]["page_numbers"] == [1]
    assert payload["chunks"][0]["retrieval_text"]
    assert payload["chunks"][0]["citations"][0]["page_number"] == 1


def test_extract_for_llm_marks_ocr_recommended_for_scanned_pdf(scanned_image_pdf: Path, tmp_path: Path) -> None:
    result = extract_for_llm(scanned_image_pdf, tmp_path / "llm")
    outputs = [Path(path) if isinstance(path, str) else path for path in result["outputs"]]
    payload = json.loads(next(path for path in outputs if path.suffix == ".json").read_text(encoding="utf-8"))
    quality = payload["document"]["extraction_quality"]
    assert quality["ocr_recommended"] is True
    assert quality["empty_pages"] == [1]
    assert quality["image_only_pages"] == [1]


def test_analyze_pdf_with_llm_writes_structured_outputs(sample_pdf: Path, tmp_path: Path, monkeypatch) -> None:
    def fake_invoke(*, model, schema, instructions, input_text):
        del model, instructions, input_text
        if schema.__name__ == "SummaryAnalysis":
            return schema(
                executive_summary="Summary",
                key_points=["Point"],
                risks=["Risk"],
                action_items=["Action"],
                citations=[{"chunk_id": "sample-c001"}],
            )
        if schema.__name__ == "EntitiesAnalysis":
            return schema(
                people=[{"value": "Jeff", "citations": [{"chunk_id": "sample-c001"}]}],
                organizations=[{"value": "PDF Toolkit", "citations": [{"chunk_id": "sample-c001"}]}],
            )
        return schema(
            answer="Answer",
            confidence="high",
            follow_up_questions=["Next?"],
            citations=[{"chunk_id": "sample-c001"}],
        )

    monkeypatch.setattr("pdf_toolkit.llm_analysis._invoke_structured_response", fake_invoke)

    summary_result = analyze_pdf_with_llm(sample_pdf, tmp_path / "summary-root", preset="summary")
    entities_result = analyze_pdf_with_llm(sample_pdf, tmp_path / "entities-root", preset="entities")
    qa_result = analyze_pdf_with_llm(sample_pdf, tmp_path / "qa-root", preset="qa", question="What is in the PDF?")

    for result in (summary_result, entities_result, qa_result):
        outputs = [Path(path) if isinstance(path, str) else path for path in result["outputs"]]
        assert {path.suffix for path in outputs} == {".json", ".md"}

    summary_payload = json.loads((tmp_path / "summary-root" / "analysis" / "sample-summary.json").read_text(encoding="utf-8"))
    entities_payload = json.loads((tmp_path / "entities-root" / "analysis" / "sample-entities.json").read_text(encoding="utf-8"))
    qa_payload = json.loads((tmp_path / "qa-root" / "analysis" / "sample-qa.json").read_text(encoding="utf-8"))

    assert summary_payload["result"]["citations"][0]["page_numbers"] == [1]
    assert entities_payload["result"]["people"][0]["citations"][0]["chunk_id"] == "sample-c001"
    assert qa_payload["result"]["confidence"] == "high"


def test_analyze_pdf_with_llm_reuses_existing_bundle(sample_pdf: Path, tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "analysis-root"
    extract_for_llm(sample_pdf, root / "llm")

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
    result = analyze_pdf_with_llm(sample_pdf, root, preset="summary")
    assert result["details"]["bundle_reused"] is True


def test_analyze_pdf_with_llm_validates_qa_question(sample_pdf: Path, tmp_path: Path) -> None:
    from pdf_toolkit.errors import ValidationError

    try:
        analyze_pdf_with_llm(sample_pdf, tmp_path / "qa-root", preset="qa")
    except ValidationError as exc:
        assert "Question is required" in str(exc)
    else:
        raise AssertionError("Expected ValidationError")


def test_analyze_pdf_with_llm_requires_openai_api_key(sample_pdf: Path, tmp_path: Path, monkeypatch) -> None:
    from pdf_toolkit.errors import DependencyMissingError

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    try:
        analyze_pdf_with_llm(sample_pdf, tmp_path / "summary-root", preset="summary")
    except DependencyMissingError as exc:
        assert "OPENAI_API_KEY" in str(exc)
    else:
        raise AssertionError("Expected DependencyMissingError")


def test_analyze_pdf_with_llm_rejects_empty_scan_context(scanned_image_pdf: Path, tmp_path: Path, monkeypatch) -> None:
    from pdf_toolkit.errors import ValidationError

    def fail_if_called(**kwargs):
        raise AssertionError("LLM invocation should not run for empty extraction bundles")

    monkeypatch.setattr("pdf_toolkit.llm_analysis._invoke_structured_response", fail_if_called)
    try:
        analyze_pdf_with_llm(scanned_image_pdf, tmp_path / "scan-summary-root", preset="summary")
    except ValidationError as exc:
        assert "run OCR first" in str(exc)
    else:
        raise AssertionError("Expected ValidationError for scan-heavy PDF with no extractable text")


def test_run_batch_generates_json_and_csv(sample_pdf: Path, table_pdf: Path, tmp_path: Path) -> None:
    input_root = tmp_path / "inputs"
    input_root.mkdir()
    sample_target = input_root / "sample.pdf"
    table_target = input_root / "table.pdf"
    shutil.copy2(sample_pdf, sample_target)
    shutil.copy2(table_pdf, table_target)

    manifest_path = tmp_path / "manifest.yaml"
    manifest = {
        "input_root": str(input_root),
        "output_root": str(tmp_path / "output"),
        "report_path": str(tmp_path / "output" / "batch-report.json"),
        "fail_fast": False,
        "jobs": [
            {
                "name": "compress-job",
                "inputs": ["sample.pdf"],
                "steps": [{"action": "compress"}, {"action": "extract_text"}],
            },
            {
                "name": "table-job",
                "inputs": ["table.pdf"],
                "steps": [{"action": "tables_extract", "format": "all"}],
            },
        ],
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = run_batch(manifest_path, load_config(tmp_path), overwrite=False)
    assert any(Path(path).suffix == ".json" for path in result["outputs"])
    assert any(Path(path).suffix == ".csv" for path in result["outputs"])


def test_build_folder_batch_manifest_runs_selected_steps(sample_pdf: Path, tmp_path: Path) -> None:
    input_root = tmp_path / "incoming"
    output_root = tmp_path / "processed"
    input_root.mkdir()
    shutil.copy2(sample_pdf, input_root / "a.pdf")
    manifest = build_folder_batch_manifest(
        input_root,
        output_root,
        steps=[{"action": "compress"}, {"action": "extract_text"}],
        recursive_inputs=True,
        file_patterns=["*.pdf"],
        job_name="gui-folder-batch",
    )
    manifest_path = write_manifest(tmp_path / "folder-batch.yaml", manifest)
    result = run_batch(manifest_path, load_config(tmp_path), overwrite=False)
    assert any(Path(path).suffix == ".json" for path in result["outputs"])
    report = yaml.safe_load((output_root / "batch-report.json").read_text(encoding="utf-8"))
    assert report["jobs"][0]["status"] == "success"


def test_build_folder_batch_manifest_runs_extract_llm_step(sample_pdf: Path, tmp_path: Path) -> None:
    input_root = tmp_path / "incoming"
    output_root = tmp_path / "processed"
    input_root.mkdir()
    shutil.copy2(sample_pdf, input_root / "a.pdf")
    manifest = build_folder_batch_manifest(
        input_root,
        output_root,
        steps=[{"action": "extract_llm", "chunk_size": 800, "overlap": 100}],
        recursive_inputs=True,
        file_patterns=["*.pdf"],
        job_name="gui-folder-batch-llm",
    )
    manifest_path = write_manifest(tmp_path / "folder-batch-llm.yaml", manifest)
    run_batch(manifest_path, load_config(tmp_path), overwrite=False)
    llm_dir = output_root / "gui-folder-batch-llm" / "a" / "llm"
    assert any(path.suffix == ".jsonl" for path in llm_dir.iterdir())


def test_build_folder_batch_manifest_runs_analyze_llm_step(sample_pdf: Path, tmp_path: Path, monkeypatch) -> None:
    input_root = tmp_path / "incoming"
    output_root = tmp_path / "processed"
    input_root.mkdir()
    shutil.copy2(sample_pdf, input_root / "a.pdf")

    def fake_invoke(*, model, schema, instructions, input_text):
        del model, instructions, input_text
        return schema(
            executive_summary="Summary",
            key_points=["Point"],
            risks=[],
            action_items=[],
            citations=[{"chunk_id": "a-c001"}],
        )

    monkeypatch.setattr("pdf_toolkit.llm_analysis._invoke_structured_response", fake_invoke)
    manifest = build_folder_batch_manifest(
        input_root,
        output_root,
        steps=[{"action": "analyze_llm", "preset": "summary", "model": "gpt-5-mini"}],
        recursive_inputs=True,
        file_patterns=["*.pdf"],
        job_name="gui-folder-batch-analyze-llm",
    )
    manifest_path = write_manifest(tmp_path / "folder-batch-analyze-llm.yaml", manifest)
    run_batch(manifest_path, load_config(tmp_path), overwrite=False)
    analysis_dir = output_root / "gui-folder-batch-analyze-llm" / "a" / "analysis"
    assert any(path.suffix == ".json" for path in analysis_dir.iterdir())


def test_run_batch_respects_non_recursive_inputs(sample_pdf: Path, tmp_path: Path) -> None:
    input_root = tmp_path / "incoming"
    nested = input_root / "nested"
    output_root = tmp_path / "processed"
    nested.mkdir(parents=True)
    shutil.copy2(sample_pdf, input_root / "root.pdf")
    shutil.copy2(sample_pdf, nested / "nested.pdf")
    manifest = build_folder_batch_manifest(
        input_root,
        output_root,
        steps=[{"action": "compress"}],
        recursive_inputs=False,
        file_patterns=["*.pdf"],
    )
    manifest_path = write_manifest(tmp_path / "non-recursive.yaml", manifest)
    run_batch(manifest_path, load_config(tmp_path), overwrite=False)
    report = yaml.safe_load((output_root / "batch-report.json").read_text(encoding="utf-8"))
    assert len(report["jobs"]) == 1
    assert report["jobs"][0]["input_path"].endswith("root.pdf")


def test_build_file_batch_manifest_runs_selected_files(sample_pdf: Path, tmp_path: Path) -> None:
    first = tmp_path / "first.pdf"
    second_dir = tmp_path / "nested"
    second_dir.mkdir()
    second = second_dir / "second.pdf"
    shutil.copy2(sample_pdf, first)
    shutil.copy2(sample_pdf, second)
    output_root = tmp_path / "processed"
    manifest = build_file_batch_manifest(
        [first, second],
        output_root,
        steps=[{"action": "compress"}],
        job_name="picked-files",
    )
    manifest_path = write_manifest(tmp_path / "picked-files.yaml", manifest)
    run_batch(manifest_path, load_config(tmp_path), overwrite=False)
    report = yaml.safe_load((output_root / "batch-report.json").read_text(encoding="utf-8"))
    assert len(report["jobs"]) == 2
    assert all(job["status"] == "success" for job in report["jobs"])


def test_scan_duplicate_pdfs_detects_exact_duplicates(sample_pdf: Path, tmp_path: Path) -> None:
    folder = tmp_path / "dupes"
    folder.mkdir()
    first = folder / "a.pdf"
    second = folder / "b.pdf"
    unique = folder / "c.pdf"
    shutil.copy2(sample_pdf, first)
    shutil.copy2(sample_pdf, second)
    unique.write_bytes(sample_pdf.read_bytes() + b"unique-tail")
    result = scan_duplicate_pdfs(folder, recursive=True)
    assert result["duplicate_group_count"] == 1
    assert result["duplicate_file_count"] == 1


def test_remove_duplicate_pdfs_deletes_extra_files(sample_pdf: Path, tmp_path: Path) -> None:
    folder = tmp_path / "dupes"
    folder.mkdir()
    first = folder / "a.pdf"
    second = folder / "b.pdf"
    shutil.copy2(sample_pdf, first)
    shutil.copy2(sample_pdf, second)
    result = remove_duplicate_pdfs(folder, recursive=True, delete_duplicates=True)
    assert result["details"]["removed_count"] == 1
    assert first.exists() or second.exists()
    assert not (first.exists() and second.exists())


def test_process_watch_folder_once(sample_pdf: Path, tmp_path: Path) -> None:
    input_dir = tmp_path / "watch"
    input_dir.mkdir()
    shutil.copy2(sample_pdf, input_dir / "incoming.pdf")

    manifest_path = tmp_path / "watch.yaml"
    manifest = {
        "output_root": str(tmp_path / "watched-output"),
        "jobs": [
            {
                "name": "watch-compress",
                "inputs": ["incoming.pdf"],
                "steps": [{"action": "compress"}],
            }
        ],
    }
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = process_watch_folder_once(input_dir, manifest_path, load_config(tmp_path), overwrite=False)
    assert any(Path(path).suffix == ".json" for path in result["outputs"])
