from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


TemplateTarget = Literal["operation", "batch"]


@dataclass(frozen=True, slots=True)
class WorkflowTemplate:
    id: str
    label: str
    description: str
    operation_id: str
    target: TemplateTarget
    values: dict[str, Any] = field(default_factory=dict)
    output_hint: str | None = None
    dependency_note: str | None = None


_TEMPLATES = (
    WorkflowTemplate(
        id="merge-invoice-packet",
        label="Merge Invoice Packet",
        description="Collect multiple invoice PDFs into one shareable packet with a single export target.",
        operation_id="merge",
        target="operation",
        values={"output": "invoice-packet.pdf"},
        output_hint="Suggested output: invoice-packet.pdf",
    ),
    WorkflowTemplate(
        id="split-page-ranges",
        label="Split By Page Ranges",
        description="Break one PDF into a clean set of ranged exports for filing, emailing, or review.",
        operation_id="split",
        target="operation",
        values={"ranges": "1-2,3-4", "output_dir": "split-pages"},
        output_hint="Suggested folder: split-pages",
    ),
    WorkflowTemplate(
        id="redact-pii-share",
        label="Redact PII For Sharing",
        description="Start with common PII patterns and export a clean copy for external sharing.",
        operation_id="redact",
        target="operation",
        values={
            "pattern": ["SSN", "DOB", "Account Number", "Email"],
            "label": "REDACTED",
            "output": "shared-redacted.pdf",
        },
        output_hint="Suggested output: shared-redacted.pdf",
    ),
    WorkflowTemplate(
        id="export-tables-spreadsheet",
        label="Export Tables To Spreadsheet Files",
        description="Extract tables into structured spreadsheet-friendly files for office reporting work.",
        operation_id="tables-extract",
        target="operation",
        values={"output_dir": "table-export", "format_name": "all", "ocr_first": False},
        output_hint="Suggested folder: table-export",
    ),
    WorkflowTemplate(
        id="ocr-scanned-documents",
        label="OCR Scanned Documents",
        description="Turn scanned PDFs into searchable files while keeping OCR clearly optional.",
        operation_id="ocr",
        target="operation",
        values={"output": "searchable-copy.pdf", "language": "eng", "skip_existing_text": True, "force": False},
        output_hint="Suggested output: searchable-copy.pdf",
        dependency_note="Requires optional OCR tools: OCRmyPDF, Tesseract, and Ghostscript.",
    ),
    WorkflowTemplate(
        id="watch-incoming-folder",
        label="Watch Incoming Folder",
        description="Set up a repeatable folder workflow for newly dropped PDFs without building a manifest by hand.",
        operation_id="batch-run",
        target="batch",
        values={
            "source_mode": "folder",
            "input_root": "",
            "input_files": [],
            "output_root": "incoming-processed",
            "report_path": "incoming-processed\\batch-report.json",
            "file_patterns": ["*.pdf"],
            "recursive_inputs": True,
            "fail_fast": False,
            "job_name": "incoming-pdf-workflow",
            "steps": [
                {"action": "compress"},
                {"action": "extract_text"},
            ],
        },
        output_hint="Suggested folder: incoming-processed",
    ),
)


def get_workflow_templates() -> list[WorkflowTemplate]:
    return list(_TEMPLATES)


def get_workflow_template(template_id: str) -> WorkflowTemplate:
    for template in _TEMPLATES:
        if template.id == template_id:
            return template
    raise KeyError(template_id)


__all__ = ["WorkflowTemplate", "get_workflow_template", "get_workflow_templates"]
