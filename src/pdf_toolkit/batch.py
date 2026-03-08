from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
import fnmatch
import time
from typing import Callable

import yaml
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from pdf_toolkit.config import ToolkitConfig
from pdf_toolkit.core import compress_pdf, ensure_dir, extract_text, inspect_pdf, render_pdf, set_metadata
from pdf_toolkit.errors import ValidationError
from pdf_toolkit.llm_analysis import DEFAULT_LLM_MODEL, analyze_pdf_with_llm
from pdf_toolkit.llm_extract import extract_for_llm
from pdf_toolkit.ocr import run_ocr
from pdf_toolkit.redaction import run_redaction
from pdf_toolkit.reporting import write_batch_csv, write_json
from pdf_toolkit.tables import extract_tables_to_files


LogCallback = Callable[[str], None]


def _emit(callback: LogCallback | None, message: str) -> None:
    if callback is not None:
        callback(message)


def load_manifest(path: Path) -> dict[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if "jobs" not in data:
        raise ValidationError("Batch manifest must include a top-level 'jobs' list.")
    return data


def write_manifest(path: Path, manifest: dict[str, object]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return path


def build_folder_batch_manifest(
    input_root: Path,
    output_root: Path,
    *,
    steps: list[dict[str, object]],
    recursive_inputs: bool,
    file_patterns: list[str] | None = None,
    report_path: Path | None = None,
    fail_fast: bool = False,
    job_name: str = "folder-batch",
) -> dict[str, object]:
    if not steps:
        raise ValidationError("Select at least one batch step.")
    patterns = [pattern.strip() for pattern in (file_patterns or ["*.pdf"]) if pattern.strip()]
    if not patterns:
        raise ValidationError("Provide at least one PDF file pattern.")
    return {
        "input_root": str(input_root.resolve()),
        "output_root": str(output_root.resolve()),
        "report_path": str((report_path or (output_root / "batch-report.json")).resolve()),
        "fail_fast": fail_fast,
        "recursive_inputs": recursive_inputs,
        "jobs": [
            {
                "name": job_name,
                "inputs": patterns,
                "steps": steps,
            }
        ],
    }


def build_file_batch_manifest(
    input_paths: list[Path],
    output_root: Path,
    *,
    steps: list[dict[str, object]],
    report_path: Path | None = None,
    fail_fast: bool = False,
    job_name: str = "selected-files-batch",
) -> dict[str, object]:
    if not input_paths:
        raise ValidationError("Select at least one PDF file.")
    if not steps:
        raise ValidationError("Select at least one batch step.")
    resolved_inputs = [path.resolve() for path in input_paths]
    try:
        input_root = Path(os.path.commonpath([str(path.parent) for path in resolved_inputs]))
    except ValueError as exc:
        raise ValidationError("Selected files must be on the same drive.") from exc
    relative_inputs = [str(path.relative_to(input_root).as_posix()) for path in resolved_inputs]
    return {
        "input_root": str(input_root),
        "output_root": str(output_root.resolve()),
        "report_path": str((report_path or (output_root / "batch-report.json")).resolve()),
        "fail_fast": fail_fast,
        "recursive_inputs": False,
        "jobs": [
            {
                "name": job_name,
                "inputs": relative_inputs,
                "steps": steps,
            }
        ],
    }


def _expand_inputs(input_root: Path, patterns: list[str], *, recursive_inputs: bool) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        if any(token in pattern for token in "*?[]"):
            files.extend(
                sorted(
                    path
                    for path in (input_root.rglob("*") if recursive_inputs else input_root.glob("*"))
                    if path.is_file() and fnmatch.fnmatch(path.relative_to(input_root).as_posix(), pattern)
                )
            )
        else:
            files.append((input_root / pattern).resolve())
    return files


def _base_output_dir(output_root: Path, job_name: str, input_path: Path) -> Path:
    return output_root / job_name / input_path.stem


def _run_step(step: dict[str, object], current_pdf: Path, base_output_dir: Path, config: ToolkitConfig) -> tuple[Path, list[str]]:
    ensure_dir(base_output_dir)
    action = str(step.get("action", "")).strip()

    if action == "ocr":
        destination = base_output_dir / f"{current_pdf.stem}-ocr.pdf"
        text_output = base_output_dir / f"{current_pdf.stem}-ocr.txt" if step.get("text_output", True) else None
        run_ocr(
            current_pdf,
            destination,
            language=str(step.get("language", config.ocr_language)),
            skip_existing_text=bool(step.get("skip_existing_text", True)),
            text_output=text_output,
            json_output=None,
            force=bool(step.get("force", False)),
            temp_dir=config.temp_dir,
        )
        outputs = [str(destination)]
        if text_output:
            outputs.append(str(text_output))
        return destination, outputs

    if action == "redact":
        destination = base_output_dir / f"{current_pdf.stem}-redacted.pdf"
        result = run_redaction(
            current_pdf,
            output_path=destination,
            patterns=list(step.get("patterns", [])),
            regex=bool(step.get("regex", False)),
            case_sensitive=bool(step.get("case_sensitive", False)),
            page_spec=str(step.get("pages")) if step.get("pages") else None,
            box_specs=list(step.get("boxes", [])),
            label=str(step.get("label")) if step.get("label") else None,
            dry_run=False,
        )
        return destination, [str(path) for path in result["outputs"]]

    if action == "extract_text":
        destination = base_output_dir / f"{current_pdf.stem}.txt"
        destination.write_text(extract_text(current_pdf), encoding="utf-8")
        return current_pdf, [str(destination)]

    if action == "extract_llm":
        llm_dir = base_output_dir / "llm"
        result = extract_for_llm(
            current_pdf,
            llm_dir,
            chunk_size=int(step.get("chunk_size", 1200)),
            overlap=int(step.get("overlap", 200)),
            include_page_markers=bool(step.get("include_page_markers", True)),
            include_metadata=bool(step.get("include_metadata", True)),
        )
        return current_pdf, [str(path) for path in result["outputs"]]

    if action == "analyze_llm":
        result = analyze_pdf_with_llm(
            current_pdf,
            base_output_dir,
            preset=str(step.get("preset", "summary")),
            question=str(step.get("question")).strip() if step.get("question") else None,
            model=str(step.get("model", DEFAULT_LLM_MODEL)),
            overwrite_bundle=True,
        )
        return current_pdf, [str(path) for path in result["outputs"]]

    if action == "tables_extract":
        table_dir = base_output_dir / "tables"
        result = extract_tables_to_files(
            current_pdf,
            table_dir,
            page_spec=str(step.get("pages")) if step.get("pages") else None,
            format_name=str(step.get("format", "all")),
            ocr_first=bool(step.get("ocr_first", False)),
            ocr_language=str(step.get("language", config.ocr_language)),
            temp_dir=config.temp_dir,
        )
        return current_pdf, [str(path) for path in result["outputs"]]

    if action == "render":
        image_dir = base_output_dir / "rendered"
        outputs = render_pdf(
            current_pdf,
            image_dir,
            dpi=int(step.get("dpi", 150)),
            page_spec=str(step.get("pages")) if step.get("pages") else None,
            image_format=str(step.get("image_format", "png")),
        )
        return current_pdf, [str(path) for path in outputs]

    if action == "compress":
        destination = base_output_dir / f"{current_pdf.stem}-compressed.pdf"
        compress_pdf(current_pdf, destination)
        return destination, [str(destination)]

    if action == "set_metadata":
        destination = base_output_dir / f"{current_pdf.stem}-metadata.pdf"
        set_metadata(current_pdf, destination, dict(step.get("values", {})), clear_existing=bool(step.get("clear_existing", False)))
        return destination, [str(destination)]

    raise ValidationError(f"Unsupported batch step action '{action}'.")


def run_batch(manifest_path: Path, config: ToolkitConfig, *, overwrite: bool) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    input_root = Path(str(manifest.get("input_root", "."))).resolve()
    output_root = Path(str(manifest.get("output_root", "output/batch"))).resolve()
    report_path = Path(str(manifest.get("report_path", output_root / "batch-report.json"))).resolve()
    csv_report_path = report_path.with_suffix(".csv")
    fail_fast = bool(manifest.get("fail_fast", False))
    recursive_inputs = bool(manifest.get("recursive_inputs", True))
    ensure_dir(output_root)

    entries: list[dict[str, object]] = []
    detailed_jobs: list[dict[str, object]] = []

    for job in list(manifest.get("jobs", [])):
        job_name = str(job.get("name", "unnamed-job"))
        inputs = _expand_inputs(input_root, list(job.get("inputs", [])), recursive_inputs=recursive_inputs)
        steps = list(job.get("steps", []))
        for input_path in inputs:
            started = time.perf_counter()
            current_pdf = input_path
            output_paths: list[str] = []
            warnings: list[str] = []
            error_message = ""
            status = "success"
            try:
                for step in steps:
                    current_pdf, step_outputs = _run_step(step, current_pdf, _base_output_dir(output_root, job_name, input_path), config)
                    output_paths.extend(step_outputs)
            except Exception as exc:
                status = "error"
                error_message = str(exc)
                if fail_fast:
                    row = {
                        "job_name": job_name,
                        "input_path": str(input_path),
                        "output_path": ";".join(output_paths),
                        "status": status,
                        "duration_ms": int((time.perf_counter() - started) * 1000),
                        "pages_processed": 0,
                        "warnings": ";".join(warnings),
                        "error_message": error_message,
                    }
                    entries.append(row)
                    detailed_jobs.append(row)
                    write_json({"jobs": detailed_jobs}, report_path)
                    write_batch_csv(entries, csv_report_path)
                    raise
            row = {
                "job_name": job_name,
                "input_path": str(input_path),
                "output_path": ";".join(output_paths),
                "status": status,
                "duration_ms": int((time.perf_counter() - started) * 1000),
                "pages_processed": inspect_pdf(input_path).page_count,
                "warnings": ";".join(warnings),
                "error_message": error_message,
            }
            entries.append(row)
            detailed_jobs.append(
                {
                    **row,
                    "step_outputs": output_paths,
                }
            )

    payload = {
        "manifest_path": str(manifest_path),
        "generated_at": datetime.utcnow().isoformat(),
        "jobs": detailed_jobs,
    }
    write_json(payload, report_path)
    write_batch_csv(entries, csv_report_path)
    return {
        "outputs": [report_path, csv_report_path],
        "details": payload,
    }


def process_watch_folder_once(
    input_dir: Path,
    manifest_path: Path,
    config: ToolkitConfig,
    *,
    overwrite: bool,
    event_callback: LogCallback | None = None,
) -> dict[str, object]:
    manifest = load_manifest(manifest_path)
    output_root = Path(str(manifest.get("output_root", "output/watch"))).resolve()
    dated_output_root = output_root / datetime.now().strftime("%Y%m%d-%H%M%S")
    manifest["input_root"] = str(input_dir.resolve())
    manifest["output_root"] = str(dated_output_root)
    temp_manifest = dated_output_root / "watch-manifest.yaml"
    ensure_dir(dated_output_root)
    _emit(event_callback, f"Preparing watch run for {input_dir}")
    temp_manifest.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = run_batch(temp_manifest, config, overwrite=overwrite)
    _emit(event_callback, f"Watch run finished with {len(result.get('outputs', []))} output artifact(s).")
    return result


class WatchFolderHandler(FileSystemEventHandler):
    def __init__(
        self,
        input_dir: Path,
        manifest_path: Path,
        config: ToolkitConfig,
        overwrite: bool,
        callback: LogCallback | None = None,
    ) -> None:
        self.input_dir = input_dir
        self.manifest_path = manifest_path
        self.config = config
        self.overwrite = overwrite
        self.callback = callback

    def on_created(self, event) -> None:
        if event.is_directory:
            return
        path = Path(event.src_path)
        if path.suffix.lower() != ".pdf":
            return
        _emit(self.callback, f"Detected {path.name}")
        process_watch_folder_once(
            self.input_dir,
            self.manifest_path,
            self.config,
            overwrite=self.overwrite,
            event_callback=self.callback,
        )


def watch_folder(
    input_dir: Path,
    manifest_path: Path,
    config: ToolkitConfig,
    *,
    overwrite: bool,
    recursive: bool,
    callback: LogCallback | None = None,
) -> None:
    observer = Observer()
    handler = WatchFolderHandler(input_dir, manifest_path, config, overwrite, callback=callback)
    observer.schedule(handler, str(input_dir), recursive=recursive)
    observer.start()
    _emit(callback, f"Watching {input_dir} (recursive={recursive})")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
