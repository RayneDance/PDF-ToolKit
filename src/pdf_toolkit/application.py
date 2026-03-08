from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
import time
from typing import Any, Callable, Literal

from pdf_toolkit.batch import process_watch_folder_once, run_batch, watch_folder
from pdf_toolkit.config import ToolkitConfig, load_config, resolve_path
from pdf_toolkit.core import (
    add_attachments,
    clear_metadata,
    compress_pdf,
    crop_pdf,
    decrypt_pdf,
    encrypt_pdf,
    extract_attachments,
    extract_images,
    extract_text,
    fill_form,
    images_to_pdf,
    inspect_pdf,
    list_attachments,
    list_bookmarks,
    list_form_fields,
    merge_pdfs,
    number_pages,
    remove_annotations,
    render_pdf,
    rotate_pdf,
    sanitize_filename,
    select_pages,
    set_metadata,
    split_pdf,
    stamp_text,
)
from pdf_toolkit.duplicates import remove_duplicate_pdfs
from pdf_toolkit.environment import collect_doctor_status
from pdf_toolkit.errors import DependencyMissingError, ValidationError
from pdf_toolkit.execution import run_mutation
from pdf_toolkit.llm_extract import extract_for_llm, llm_output_paths
from pdf_toolkit.ocr import run_ocr, scan_detect
from pdf_toolkit.redaction import run_redaction
from pdf_toolkit.tables import extract_tables_to_files

DEFAULT_LLM_MODEL = "gpt-5-mini"

FieldKind = Literal[
    "text",
    "number",
    "checkbox",
    "choice",
    "file",
    "directory",
    "password",
    "key_value_list",
    "page_spec",
    "redaction_boxes",
]
NumberMode = Literal["int", "float"]
PathRole = Literal["input", "output"]
InputMode = Literal["none", "single", "multiple", "directory"]
OperationHandler = Callable[["JobRequest", ToolkitConfig], dict[str, object]]


@dataclass(slots=True)
class OperationChoice:
    value: str
    label: str


@dataclass(slots=True)
class OperationField:
    name: str
    label: str
    kind: FieldKind
    required: bool = False
    help: str = ""
    default: Any = None
    multiple: bool = False
    choices: list[OperationChoice] = field(default_factory=list)
    placeholder: str | None = None
    path_role: PathRole | None = None
    number_mode: NumberMode = "int"
    min_value: float | None = None
    max_value: float | None = None


@dataclass(slots=True)
class OperationDefinition:
    id: str
    label: str
    category: str
    input_mode: InputMode
    description: str
    fields: list[OperationField]
    supports_preview: bool
    supports_report: bool
    mutating: bool
    preview_field: str | None = None


@dataclass(slots=True)
class JobRequest:
    operation_id: str
    values: dict[str, Any]
    report_path: Path | None = None
    overwrite: bool = False


@dataclass(slots=True)
class JobResult:
    operation_id: str
    status: str
    outputs: list[Path]
    warnings: list[str]
    details: dict[str, Any]
    error: str | None
    duration_ms: int


@dataclass(slots=True)
class _OperationRecord:
    definition: OperationDefinition
    handler: OperationHandler


def _choice(value: str, label: str | None = None) -> OperationChoice:
    return OperationChoice(value=value, label=label or value)


def _field(
    name: str,
    label: str,
    kind: FieldKind,
    *,
    required: bool = False,
    help: str = "",
    default: Any = None,
    multiple: bool = False,
    choices: list[OperationChoice] | None = None,
    placeholder: str | None = None,
    path_role: PathRole | None = None,
    number_mode: NumberMode = "int",
    min_value: float | None = None,
    max_value: float | None = None,
) -> OperationField:
    return OperationField(
        name=name,
        label=label,
        kind=kind,
        required=required,
        help=help,
        default=default,
        multiple=multiple,
        choices=list(choices or []),
        placeholder=placeholder,
        path_role=path_role,
        number_mode=number_mode,
        min_value=min_value,
        max_value=max_value,
    )


def _serialize(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): _serialize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_serialize(item) for item in value]
    return value


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _as_path(value: Any) -> Path:
    if isinstance(value, Path):
        return value
    if value is None:
        raise ValidationError("Expected a path value.")
    text = str(value).strip()
    if not text:
        raise ValidationError("Expected a path value.")
    return Path(text)


def _coerce_key_values(raw: Any) -> dict[str, str]:
    if raw in (None, "", []):
        return {}
    if isinstance(raw, dict):
        return {str(key).strip(): str(value) for key, value in raw.items() if str(key).strip()}
    pairs: dict[str, str] = {}
    items = raw if isinstance(raw, list) else [raw]
    for item in items:
        if isinstance(item, tuple) and len(item) == 2:
            key, value = item
        else:
            text = str(item)
            if "=" not in text:
                raise ValidationError(f"Expected KEY=VALUE entry, got '{text}'.")
            key, value = text.split("=", maxsplit=1)
        key = str(key).strip()
        if not key:
            raise ValidationError("Key/value entry has an empty key.")
        pairs[key] = str(value)
    return pairs


def _coerce_number(raw: Any, *, mode: NumberMode) -> int | float | None:
    if raw in (None, ""):
        return None
    return int(raw) if mode == "int" else float(raw)


def _coerce_field_value(field_def: OperationField, raw: Any) -> Any:
    if field_def.kind in {"file", "directory"}:
        if field_def.multiple:
            items = raw or []
            if not isinstance(items, list):
                items = [items]
            return [_as_path(item) for item in items if not _is_empty(item)]
        if _is_empty(raw):
            return None
        return _as_path(raw)

    if field_def.kind == "checkbox":
        return bool(raw)

    if field_def.kind == "number":
        return _coerce_number(raw, mode=field_def.number_mode)

    if field_def.kind == "key_value_list":
        return _coerce_key_values(raw)

    if field_def.kind in {"redaction_boxes", "page_spec"}:
        if field_def.multiple:
            items = raw or []
            if not isinstance(items, list):
                items = [items]
            return [str(item).strip() for item in items if str(item).strip()]
        return None if _is_empty(raw) else str(raw).strip()

    if field_def.multiple:
        items = raw or []
        if not isinstance(items, list):
            items = [items]
        return [str(item) for item in items if not _is_empty(item)]

    if _is_empty(raw):
        return None
    return str(raw)


def _resolve_field_path(value: Any, config: ToolkitConfig) -> Any:
    if isinstance(value, list):
        return [item if isinstance(item, Path) else _as_path(item) for item in value]
    if value is None:
        return None
    path = value if isinstance(value, Path) else _as_path(value)
    return resolve_path(path, config)


def prepare_request(
    operation_id: str,
    raw_values: dict[str, Any],
    *,
    report_path: Path | str | None = None,
    overwrite: bool = False,
    cwd: Path | None = None,
) -> JobRequest:
    definition = get_operation_definition(operation_id)
    config = load_config(cwd or Path.cwd())
    values: dict[str, Any] = {}

    for field_def in definition.fields:
        raw = raw_values.get(field_def.name, field_def.default)
        value = _coerce_field_value(field_def, raw)
        if field_def.required and _is_empty(value):
            raise ValidationError(f"{field_def.label} is required.")
        if field_def.kind in {"file", "directory"} and field_def.path_role == "output":
            value = _resolve_field_path(value, config)
        values[field_def.name] = value

    if operation_id == "analyze-llm" and values.get("preset") == "qa" and _is_empty(values.get("question")):
        raise ValidationError("Question is required when Preset is QA.")

    resolved_report = resolve_path(_as_path(report_path), config) if report_path else None
    return JobRequest(operation_id=operation_id, values=values, report_path=resolved_report, overwrite=overwrite)


def get_operation_definitions() -> list[OperationDefinition]:
    return [record.definition for record in _OPERATION_REGISTRY.values()]


def get_operation_definition(operation_id: str) -> OperationDefinition:
    try:
        return _OPERATION_REGISTRY[operation_id].definition
    except KeyError as exc:
        raise ValidationError(f"Unsupported operation '{operation_id}'.") from exc


def execute_job_or_raise(request: JobRequest, *, cwd: Path | None = None) -> JobResult:
    operation = _OPERATION_REGISTRY[request.operation_id]
    started = time.perf_counter()
    config = load_config(cwd or Path.cwd())
    payload = operation.handler(request, config)
    outputs = [_as_path(path) for path in payload.get("outputs", [])]
    warnings = [str(item) for item in payload.get("warnings", [])]
    details = {str(key): _serialize(value) for key, value in dict(payload.get("details", {})).items()}
    return JobResult(
        operation_id=request.operation_id,
        status="success",
        outputs=outputs,
        warnings=warnings,
        details=details,
        error=None,
        duration_ms=int((time.perf_counter() - started) * 1000),
    )


def execute_job(request: JobRequest, *, cwd: Path | None = None) -> JobResult:
    started = time.perf_counter()
    try:
        return execute_job_or_raise(request, cwd=cwd)
    except Exception as exc:
        return JobResult(
            operation_id=request.operation_id,
            status="error",
            outputs=[],
            warnings=[],
            details={},
            error=str(exc),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )


def _run_mutating_job(
    request: JobRequest,
    config: ToolkitConfig,
    *,
    input_paths: list[Path],
    planned_outputs: list[Path],
    action: Callable[[], dict[str, object]],
) -> dict[str, object]:
    return run_mutation(
        command=request.operation_id,
        input_paths=input_paths,
        planned_outputs=planned_outputs,
        report_path=request.report_path,
        overwrite=request.overwrite or config.overwrite,
        action=action,
    )


def _doctor_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    del config
    feature = request.values["feature"] or "all"
    statuses = collect_doctor_status(str(feature))
    missing_required = any(not status.available and status.required for status in statuses)
    missing_optional = any(not status.available and not status.required for status in statuses)
    return {
        "outputs": [],
        "details": {
            "feature": feature,
            "statuses": [_serialize(status) for status in statuses],
            "missing": missing_required,
            "missing_required": missing_required,
            "missing_optional": missing_optional,
        },
    }


def _scan_detect_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    del config
    input_path = request.values["input_path"]
    assert isinstance(input_path, Path)
    return {"outputs": [], "details": scan_detect(input_path)}


def _inspect_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    del config
    input_path = request.values["input_path"]
    assert isinstance(input_path, Path)
    return {"outputs": [], "details": _serialize(inspect_pdf(input_path))}


def _merge_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    inputs = request.values["inputs"]
    output = request.values["output"]
    assert isinstance(inputs, list)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=inputs,
        planned_outputs=[output],
        action=lambda: (merge_pdfs(inputs, output), {"outputs": [output], "details": {"input_count": len(inputs)}})[1],
    )


def _split_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output_dir = request.values["output_dir"]
    assert isinstance(input_path, Path)
    assert isinstance(output_dir, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[],
        action=lambda: {
            "outputs": split_pdf(
                input_path,
                output_dir,
                ranges=request.values["ranges"],
                every_page=bool(request.values["every_page"]),
            ),
            "details": {"ranges": request.values["ranges"], "every_page": bool(request.values["every_page"])},
        },
    )


def _select_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (
            select_pages(input_path, output, str(request.values["pages"])),
            {"outputs": [output], "details": {"pages": request.values["pages"]}},
        )[1],
    )


def _rotate_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (
            rotate_pdf(
                input_path,
                output,
                int(request.values["degrees"]),
                request.values["pages"],
            ),
            {
                "outputs": [output],
                "details": {"degrees": request.values["degrees"], "pages": request.values["pages"]},
            },
        )[1],
    )


def _extract_text_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    if output is None and request.report_path is None:
        return {"outputs": [], "details": {"text": extract_text(input_path)}}

    planned_outputs = [output] if isinstance(output, Path) else []

    def action() -> dict[str, object]:
        text = extract_text(input_path)
        if isinstance(output, Path):
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(text, encoding="utf-8")
        return {
            "outputs": [output] if isinstance(output, Path) else [],
            "details": {
                "mode": "file" if isinstance(output, Path) else "report-only",
                "text": text,
            },
        }

    return _run_mutating_job(request, config, input_paths=[input_path], planned_outputs=planned_outputs, action=action)


def _extract_llm_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output_dir = request.values["output_dir"]
    assert isinstance(input_path, Path)
    assert isinstance(output_dir, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=llm_output_paths(input_path, output_dir),
        action=lambda: extract_for_llm(
            input_path,
            output_dir,
            chunk_size=int(request.values["chunk_size"]),
            overlap=int(request.values["overlap"]),
            include_page_markers=bool(request.values["include_page_markers"]),
            include_metadata=bool(request.values["include_metadata"]),
        ),
    )


def _llm_analysis_output_paths(input_path: Path, output_dir: Path, preset: str) -> list[Path]:
    normalized_document_id = sanitize_filename(input_path.stem.lower(), "document")
    analysis_dir = output_dir / "analysis"
    return [
        analysis_dir / f"{normalized_document_id}-{preset}.json",
        analysis_dir / f"{normalized_document_id}-{preset}.md",
    ]


def _load_llm_analysis_tools() -> Callable[..., dict[str, object]]:
    try:
        from pdf_toolkit.llm_analysis import analyze_pdf_with_llm
    except ImportError as exc:
        raise DependencyMissingError(
            "LLM analysis requires the optional `llm` extras. Install with `python -m pip install -e .[llm]`."
        ) from exc
    return analyze_pdf_with_llm


def _analyze_llm_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    analyze_pdf_with_llm = _load_llm_analysis_tools()
    input_path = request.values["input_path"]
    output_dir = request.values["output_dir"]
    preset = str(request.values["preset"] or "summary")
    question = request.values["question"]
    model = str(request.values["model"] or DEFAULT_LLM_MODEL)
    assert isinstance(input_path, Path)
    assert isinstance(output_dir, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=_llm_analysis_output_paths(input_path, output_dir, preset),
        action=lambda: analyze_pdf_with_llm(
            input_path,
            output_dir,
            preset=preset,
            question=str(question) if question else None,
            model=model,
            overwrite_bundle=request.overwrite or config.overwrite,
        ),
    )


def _protect_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (
            encrypt_pdf(input_path, output, str(request.values["password"]), request.values["owner_password"]),
            {"outputs": [output], "details": {}},
        )[1],
    )


def _unlock_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (decrypt_pdf(input_path, output, str(request.values["password"])), {"outputs": [output], "details": {}})[1],
    )


def _stamp_text_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (
            stamp_text(
                input_path,
                output,
                str(request.values["text"]),
                font_size=int(request.values["font_size"]),
                opacity=float(request.values["opacity"]),
                page_spec=request.values["pages"],
            ),
            {"outputs": [output], "details": {"pages": request.values["pages"], "text": request.values["text"]}},
        )[1],
    )


def _set_metadata_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    values = request.values["values"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    assert isinstance(values, dict)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (
            set_metadata(input_path, output, values, clear_existing=bool(request.values["clear_existing"])),
            {
                "outputs": [output],
                "details": {"keys": sorted(values), "clear_existing": bool(request.values["clear_existing"])},
            },
        )[1],
    )


def _clear_metadata_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (clear_metadata(input_path, output), {"outputs": [output], "details": {}})[1],
    )


def _compress_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (compress_pdf(input_path, output), {"outputs": [output], "details": {}})[1],
    )


def _number_pages_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (
            number_pages(
                input_path,
                output,
                format_text=str(request.values["format_text"]),
                start_number=int(request.values["start_number"]),
                page_spec=request.values["pages"],
                position=str(request.values["position"]),
                margin=float(request.values["margin"]),
                font_size=int(request.values["font_size"]),
                opacity=float(request.values["opacity"]),
            ),
            {"outputs": [output], "details": {"position": request.values["position"]}},
        )[1],
    )


def _crop_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (
            crop_pdf(
                input_path,
                output,
                left=float(request.values["left"] or 0.0),
                right=float(request.values["right"] or 0.0),
                top=float(request.values["top"] or 0.0),
                bottom=float(request.values["bottom"] or 0.0),
                page_spec=request.values["pages"],
            ),
            {"outputs": [output], "details": {"pages": request.values["pages"]}},
        )[1],
    )


def _render_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output_dir = request.values["output_dir"]
    assert isinstance(input_path, Path)
    assert isinstance(output_dir, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[],
        action=lambda: {
            "outputs": render_pdf(
                input_path,
                output_dir,
                dpi=int(request.values["dpi"]),
                page_spec=request.values["pages"],
                image_format=str(request.values["image_format"]),
            ),
            "details": {"dpi": request.values["dpi"], "image_format": request.values["image_format"]},
        },
    )


def _extract_images_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output_dir = request.values["output_dir"]
    assert isinstance(input_path, Path)
    assert isinstance(output_dir, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[],
        action=lambda: {
            "outputs": extract_images(input_path, output_dir, page_spec=request.values["pages"]),
            "details": {"pages": request.values["pages"]},
        },
    )


def _images_to_pdf_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    inputs = request.values["inputs"]
    output = request.values["output"]
    assert isinstance(inputs, list)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=inputs,
        planned_outputs=[output],
        action=lambda: (images_to_pdf(inputs, output), {"outputs": [output], "details": {"input_count": len(inputs)}})[1],
    )


def _attachments_list_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    del config
    input_path = request.values["input_path"]
    assert isinstance(input_path, Path)
    return {"outputs": [], "details": {"attachments": [_serialize(item) for item in list_attachments(input_path)]}}


def _attachments_add_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    attachments = request.values["attachments"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(attachments, list)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path, *attachments],
        planned_outputs=[output],
        action=lambda: (
            add_attachments(input_path, output, attachments),
            {"outputs": [output], "details": {"attachment_count": len(attachments)}},
        )[1],
    )


def _attachments_extract_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output_dir = request.values["output_dir"]
    assert isinstance(input_path, Path)
    assert isinstance(output_dir, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[],
        action=lambda: {"outputs": extract_attachments(input_path, output_dir), "details": {}},
    )


def _form_fields_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    del config
    input_path = request.values["input_path"]
    assert isinstance(input_path, Path)
    return {"outputs": [], "details": {"fields": [_serialize(item) for item in list_form_fields(input_path)]}}


def _fill_form_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    values = request.values["values"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    assert isinstance(values, dict)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (fill_form(input_path, output, values), {"outputs": [output], "details": {"fields": sorted(values)}})[1],
    )


def _bookmarks_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    del config
    input_path = request.values["input_path"]
    assert isinstance(input_path, Path)
    return {"outputs": [], "details": {"bookmarks": list_bookmarks(input_path)}}


def _remove_annotations_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output],
        action=lambda: (
            remove_annotations(input_path, output, page_spec=request.values["pages"]),
            {"outputs": [output], "details": {"pages": request.values["pages"]}},
        )[1],
    )


def _ocr_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    text_output = request.values["text_output"]
    json_output = request.values["json_output"]
    assert isinstance(input_path, Path)
    assert isinstance(output, Path)
    planned_outputs = [path for path in [output, text_output, json_output] if isinstance(path, Path)]
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=planned_outputs,
        action=lambda: run_ocr(
            input_path,
            output,
            language=str(request.values["language"] or config.ocr_language),
            skip_existing_text=bool(request.values["skip_existing_text"]),
            text_output=text_output,
            json_output=json_output,
            force=bool(request.values["force"]),
            temp_dir=config.temp_dir,
        ),
    )


def _redact_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output = request.values["output"]
    patterns = request.values["pattern"]
    box_specs = request.values["box"]
    assert isinstance(input_path, Path)
    assert isinstance(patterns, list)
    assert isinstance(box_specs, list)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[output] if isinstance(output, Path) else [],
        action=lambda: run_redaction(
            input_path,
            output_path=output,
            patterns=patterns,
            regex=bool(request.values["regex"]),
            case_sensitive=bool(request.values["case_sensitive"]),
            page_spec=request.values["pages"],
            box_specs=box_specs,
            label=request.values["label"],
            dry_run=bool(request.values["dry_run"]),
        ),
    )


def _tables_extract_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_path = request.values["input_path"]
    output_dir = request.values["output_dir"]
    assert isinstance(input_path, Path)
    assert isinstance(output_dir, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[input_path],
        planned_outputs=[],
        action=lambda: extract_tables_to_files(
            input_path,
            output_dir,
            page_spec=request.values["pages"],
            format_name=str(request.values["format_name"]),
            ocr_first=bool(request.values["ocr_first"]),
            ocr_language=config.ocr_language,
            temp_dir=config.temp_dir,
        ),
    )


def _batch_run_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    manifest_path = request.values["manifest_path"]
    assert isinstance(manifest_path, Path)
    return _run_mutating_job(
        request,
        config,
        input_paths=[manifest_path],
        planned_outputs=[],
        action=lambda: run_batch(manifest_path, config, overwrite=request.overwrite or config.overwrite),
    )


def _watch_folder_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    input_dir = request.values["input_dir"]
    manifest_path = request.values["manifest_path"]
    recursive = bool(request.values["recursive"])
    assert isinstance(input_dir, Path)
    assert isinstance(manifest_path, Path)
    if bool(request.values["once"]):
        return _run_mutating_job(
            request,
            config,
            input_paths=[input_dir, manifest_path],
            planned_outputs=[],
            action=lambda: process_watch_folder_once(
                input_dir,
                manifest_path,
                config,
                overwrite=request.overwrite or config.overwrite,
            ),
        )
    watch_folder(input_dir, manifest_path, config, overwrite=request.overwrite or config.overwrite, recursive=recursive)
    return {
        "outputs": [],
        "details": {
            "watching": True,
            "input_dir": input_dir,
            "manifest_path": manifest_path,
            "recursive": recursive,
        },
    }


def _deduplicate_folder_handler(request: JobRequest, config: ToolkitConfig) -> dict[str, object]:
    del config
    input_dir = request.values["input_dir"]
    assert isinstance(input_dir, Path)
    return remove_duplicate_pdfs(
        input_dir,
        recursive=bool(request.values["recursive"]),
        delete_duplicates=bool(request.values["delete_duplicates"]),
    )


def _build_registry() -> dict[str, _OperationRecord]:
    records: list[_OperationRecord] = [
        _OperationRecord(
            OperationDefinition(
                id="doctor",
                label="Doctor",
                category="Diagnostics",
                input_mode="none",
                description="Validate optional dependencies.",
                fields=[
                    _field(
                        "feature",
                        "Feature Set",
                        "choice",
                        default="all",
                        choices=[
                            _choice("all", "All"),
                            _choice("ocr", "OCR"),
                            _choice("redaction", "Redaction"),
                            _choice("tables", "Tables"),
                            _choice("batch", "Batch"),
                            _choice("render", "Render"),
                            _choice("llm", "LLM"),
                        ],
                    )
                ],
                supports_preview=False,
                supports_report=False,
                mutating=False,
            ),
            _doctor_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="scan-detect",
                label="Scan Detect",
                category="Diagnostics",
                input_mode="single",
                description="Classify pages as text, image, or mixed.",
                fields=[_field("input_path", "Input PDF", "file", required=True, path_role="input")],
                supports_preview=True,
                supports_report=False,
                mutating=False,
                preview_field="input_path",
            ),
            _scan_detect_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="inspect",
                label="Inspect",
                category="Diagnostics",
                input_mode="single",
                description="Summarize a PDF.",
                fields=[_field("input_path", "Input PDF", "file", required=True, path_role="input")],
                supports_preview=True,
                supports_report=False,
                mutating=False,
                preview_field="input_path",
            ),
            _inspect_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="merge",
                label="Combine PDFs",
                category="Document",
                input_mode="multiple",
                description="Combine multiple PDFs into one clean document.",
                fields=[
                    _field("inputs", "PDFs To Combine", "file", required=True, multiple=True, path_role="input"),
                    _field("output", "Save Combined PDF As", "file", required=True, path_role="output"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="inputs",
            ),
            _merge_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="split",
                label="Split Pages",
                category="Document",
                input_mode="single",
                description="Split one PDF by ranges or export every page separately.",
                fields=[
                    _field("input_path", "Source PDF", "file", required=True, path_role="input"),
                    _field("output_dir", "Save Split Files To", "directory", required=True, path_role="output"),
                    _field("ranges", "Ranges", "text", placeholder="1-3,4-6"),
                    _field("every_page", "Every Page", "checkbox", default=False),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _split_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="select",
                label="Select Pages",
                category="Document",
                input_mode="single",
                description="Reorder or subset pages.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("pages", "Pages", "page_spec", required=True, placeholder="3,1-2"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _select_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="rotate",
                label="Rotate",
                category="Document",
                input_mode="single",
                description="Rotate pages by a multiple of 90.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("degrees", "Degrees", "number", required=True, default=90, number_mode="int"),
                    _field("pages", "Pages", "page_spec", placeholder="1,3-4"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _rotate_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="extract-text",
                label="Export Text",
                category="Text And Images",
                input_mode="single",
                description="Export PDF text to the app or a text file.",
                fields=[
                    _field("input_path", "Source PDF", "file", required=True, path_role="input"),
                    _field("output", "Save Text File As", "file", path_role="output"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _extract_text_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="extract-llm",
                label="Extract For LLM",
                category="Text And Images",
                input_mode="single",
                description="Extract Markdown, JSON, and chunk files optimized for LLM and embedding workflows.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output_dir", "Output Folder", "directory", required=True, path_role="output"),
                    _field("chunk_size", "Chunk Size", "number", default=1200, number_mode="int", min_value=1),
                    _field("overlap", "Overlap", "number", default=200, number_mode="int", min_value=0),
                    _field("include_page_markers", "Include Page Markers", "checkbox", default=True),
                    _field("include_metadata", "Include Metadata", "checkbox", default=True),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _extract_llm_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="analyze-llm",
                label="Analyze With LLM",
                category="Text And Images",
                input_mode="single",
                description="Run optional OpenAI-powered analysis on a reusable local LLM bundle.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output_dir", "Output Folder", "directory", required=True, path_role="output"),
                    _field(
                        "preset",
                        "Preset",
                        "choice",
                        default="summary",
                        choices=[
                            _choice("summary", "Summary"),
                            _choice("entities", "Entities"),
                            _choice("qa", "Q&A"),
                        ],
                    ),
                    _field("question", "Question", "text", help="Required when Preset is Q&A."),
                    _field("model", "Model", "text", default=DEFAULT_LLM_MODEL),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _analyze_llm_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="protect",
                label="Protect",
                category="Security",
                input_mode="single",
                description="Encrypt a PDF.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("password", "User Password", "password", required=True),
                    _field("owner_password", "Owner Password", "password"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _protect_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="unlock",
                label="Unlock",
                category="Security",
                input_mode="single",
                description="Decrypt a PDF.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("password", "Password", "password", required=True),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _unlock_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="stamp-text",
                label="Stamp Text",
                category="Security",
                input_mode="single",
                description="Apply a text watermark.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("text", "Text", "text", required=True),
                    _field("pages", "Pages", "page_spec"),
                    _field("font_size", "Font Size", "number", default=48, number_mode="int"),
                    _field("opacity", "Opacity", "number", default=0.2, number_mode="float"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _stamp_text_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="set-metadata",
                label="Set Metadata",
                category="Metadata Forms",
                input_mode="single",
                description="Update document metadata.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("values", "Metadata", "key_value_list", required=True),
                    _field("clear_existing", "Clear Existing Metadata", "checkbox", default=False),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _set_metadata_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="clear-metadata",
                label="Clear Metadata",
                category="Metadata Forms",
                input_mode="single",
                description="Clear document metadata.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _clear_metadata_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="compress",
                label="Compress",
                category="Document",
                input_mode="single",
                description="Compress a PDF.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _compress_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="number-pages",
                label="Number Pages",
                category="Text And Images",
                input_mode="single",
                description="Overlay page numbers.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("pages", "Pages", "page_spec"),
                    _field("format_text", "Format", "text", default="Page {page} of {total}"),
                    _field("start_number", "Start Number", "number", default=1, number_mode="int"),
                    _field(
                        "position",
                        "Position",
                        "choice",
                        default="bottom-right",
                        choices=[
                            _choice("bottom-right"),
                            _choice("bottom-center"),
                            _choice("bottom-left"),
                            _choice("top-right"),
                            _choice("top-center"),
                            _choice("top-left"),
                        ],
                    ),
                    _field("margin", "Margin", "number", default=36.0, number_mode="float"),
                    _field("font_size", "Font Size", "number", default=10, number_mode="int"),
                    _field("opacity", "Opacity", "number", default=0.85, number_mode="float"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _number_pages_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="crop",
                label="Crop",
                category="Document",
                input_mode="single",
                description="Crop page margins.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("pages", "Pages", "page_spec"),
                    _field("left", "Left", "number", default=0.0, number_mode="float"),
                    _field("right", "Right", "number", default=0.0, number_mode="float"),
                    _field("top", "Top", "number", default=0.0, number_mode="float"),
                    _field("bottom", "Bottom", "number", default=0.0, number_mode="float"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _crop_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="render",
                label="Render",
                category="Text And Images",
                input_mode="single",
                description="Render pages to images.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output_dir", "Output Folder", "directory", required=True, path_role="output"),
                    _field("pages", "Pages", "page_spec"),
                    _field("dpi", "DPI", "number", default=150, number_mode="int"),
                    _field(
                        "image_format",
                        "Image Format",
                        "choice",
                        default="png",
                        choices=[_choice("png", "PNG"), _choice("jpg", "JPG"), _choice("jpeg", "JPEG")],
                    ),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _render_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="extract-images",
                label="Extract Images",
                category="Text And Images",
                input_mode="single",
                description="Extract embedded images.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output_dir", "Output Folder", "directory", required=True, path_role="output"),
                    _field("pages", "Pages", "page_spec"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _extract_images_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="images-to-pdf",
                label="Images To PDF",
                category="Text And Images",
                input_mode="multiple",
                description="Build a PDF from images.",
                fields=[
                    _field("inputs", "Input Images", "file", required=True, multiple=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                ],
                supports_preview=False,
                supports_report=True,
                mutating=True,
            ),
            _images_to_pdf_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="attachments-list",
                label="List Attachments",
                category="Metadata Forms",
                input_mode="single",
                description="List embedded attachments.",
                fields=[_field("input_path", "Input PDF", "file", required=True, path_role="input")],
                supports_preview=True,
                supports_report=False,
                mutating=False,
                preview_field="input_path",
            ),
            _attachments_list_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="attachments-add",
                label="Add Attachments",
                category="Metadata Forms",
                input_mode="multiple",
                description="Attach files to a PDF.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("attachments", "Files To Attach", "file", required=True, multiple=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _attachments_add_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="attachments-extract",
                label="Extract Attachments",
                category="Metadata Forms",
                input_mode="single",
                description="Extract attachments to disk.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output_dir", "Output Folder", "directory", required=True, path_role="output"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _attachments_extract_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="form-fields",
                label="Form Fields",
                category="Metadata Forms",
                input_mode="single",
                description="List AcroForm fields.",
                fields=[_field("input_path", "Input PDF", "file", required=True, path_role="input")],
                supports_preview=True,
                supports_report=False,
                mutating=False,
                preview_field="input_path",
            ),
            _form_fields_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="fill-form",
                label="Fill Form",
                category="Metadata Forms",
                input_mode="single",
                description="Fill AcroForm field values.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("values", "Field Values", "key_value_list", required=True),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _fill_form_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="bookmarks",
                label="Bookmarks",
                category="Metadata Forms",
                input_mode="single",
                description="List bookmarks.",
                fields=[_field("input_path", "Input PDF", "file", required=True, path_role="input")],
                supports_preview=True,
                supports_report=False,
                mutating=False,
                preview_field="input_path",
            ),
            _bookmarks_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="remove-annotations",
                label="Remove Annotations",
                category="Security",
                input_mode="single",
                description="Remove annotations from selected pages.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", required=True, path_role="output"),
                    _field("pages", "Pages", "page_spec"),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _remove_annotations_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="ocr",
                label="OCR Scans",
                category="OCR And Redaction",
                input_mode="single",
                description="Turn a scanned PDF into a searchable document.",
                fields=[
                    _field("input_path", "Source PDF", "file", required=True, path_role="input"),
                    _field("output", "Save Searchable PDF As", "file", required=True, path_role="output"),
                    _field("language", "Language", "text", placeholder="eng"),
                    _field("skip_existing_text", "Skip Existing Text", "checkbox", default=False),
                    _field("text_output", "Text Output", "file", path_role="output"),
                    _field("json_output", "JSON Output", "file", path_role="output"),
                    _field("force", "Force OCR", "checkbox", default=False),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _ocr_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="redact",
                label="Redact",
                category="OCR And Redaction",
                input_mode="single",
                description="Apply text or box redactions.",
                fields=[
                    _field("input_path", "Input PDF", "file", required=True, path_role="input"),
                    _field("output", "Output PDF", "file", path_role="output"),
                    _field("pattern", "Patterns", "text", multiple=True, placeholder="SSN"),
                    _field("regex", "Regex", "checkbox", default=False),
                    _field("case_sensitive", "Case Sensitive", "checkbox", default=False),
                    _field("pages", "Pages", "page_spec"),
                    _field("box", "Redaction Boxes", "redaction_boxes", multiple=True),
                    _field("label", "Label", "text"),
                    _field("dry_run", "Dry Run", "checkbox", default=False),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _redact_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="tables-extract",
                label="Export Tables",
                category="Text And Images",
                input_mode="single",
                description="Export detected tables into spreadsheet-friendly files.",
                fields=[
                    _field("input_path", "Source PDF", "file", required=True, path_role="input"),
                    _field("output_dir", "Save Table Files To", "directory", required=True, path_role="output"),
                    _field("pages", "Pages", "page_spec"),
                    _field(
                        "format_name",
                        "Format",
                        "choice",
                        default="csv",
                        choices=[_choice("csv", "CSV"), _choice("xlsx", "XLSX"), _choice("json", "JSON"), _choice("all", "All")],
                    ),
                    _field("ocr_first", "OCR First", "checkbox", default=False),
                ],
                supports_preview=True,
                supports_report=True,
                mutating=True,
                preview_field="input_path",
            ),
            _tables_extract_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="batch-run",
                label="Run Folder Workflow",
                category="Automation",
                input_mode="single",
                description="Run a repeatable workflow manifest for files or folders.",
                fields=[_field("manifest_path", "Manifest", "file", required=True, path_role="input")],
                supports_preview=False,
                supports_report=True,
                mutating=True,
            ),
            _batch_run_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="watch-folder",
                label="Watch Incoming Folder",
                category="Automation",
                input_mode="directory",
                description="Watch a folder and process new PDFs as they arrive.",
                fields=[
                    _field("input_dir", "Input Folder", "directory", required=True, path_role="input"),
                    _field("manifest_path", "Manifest", "file", required=True, path_role="input"),
                    _field("once", "Run Once", "checkbox", default=False),
                    _field("recursive", "Recursive", "checkbox", default=False),
                ],
                supports_preview=False,
                supports_report=False,
                mutating=True,
            ),
            _watch_folder_handler,
        ),
        _OperationRecord(
            OperationDefinition(
                id="deduplicate-folder",
                label="Deduplicate Folder",
                category="Automation",
                input_mode="directory",
                description="Scan a folder for duplicate PDFs and optionally remove the extra copies.",
                fields=[
                    _field("input_dir", "Input Folder", "directory", required=True, path_role="input"),
                    _field("recursive", "Include Subfolders", "checkbox", default=True),
                    _field("delete_duplicates", "Remove Duplicate Files", "checkbox", default=False),
                ],
                supports_preview=False,
                supports_report=True,
                mutating=True,
            ),
            _deduplicate_folder_handler,
        ),
    ]
    return {record.definition.id: record for record in records}


_OPERATION_REGISTRY = _build_registry()


__all__ = [
    "JobRequest",
    "JobResult",
    "OperationChoice",
    "OperationDefinition",
    "OperationField",
    "execute_job",
    "execute_job_or_raise",
    "get_operation_definition",
    "get_operation_definitions",
    "prepare_request",
]
