from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Literal
import json
import os
import re

from pydantic import BaseModel, Field

from pdf_toolkit.core import ensure_dir, sanitize_filename
from pdf_toolkit.errors import DependencyMissingError, ProcessingFailureError, ValidationError
from pdf_toolkit.llm_extract import extract_for_llm, llm_output_paths, load_llm_bundle

DEFAULT_LLM_MODEL = "gpt-5-mini"
SINGLE_PASS_TOKEN_LIMIT = 12000
SECTION_GROUP_TOKEN_LIMIT = 7000
QA_CONTEXT_TOKEN_LIMIT = 5000


class CitationReference(BaseModel):
    chunk_id: str = Field(description="Chunk identifier from the provided context.")


class SummaryAnalysis(BaseModel):
    executive_summary: str
    key_points: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    action_items: list[str] = Field(default_factory=list)
    citations: list[CitationReference] = Field(default_factory=list)


class EntityItem(BaseModel):
    value: str
    citations: list[CitationReference] = Field(default_factory=list)


class EntitiesAnalysis(BaseModel):
    people: list[EntityItem] = Field(default_factory=list)
    organizations: list[EntityItem] = Field(default_factory=list)
    dates: list[EntityItem] = Field(default_factory=list)
    amounts: list[EntityItem] = Field(default_factory=list)
    locations: list[EntityItem] = Field(default_factory=list)


class QAAnalysis(BaseModel):
    answer: str
    confidence: Literal["low", "medium", "high"]
    follow_up_questions: list[str] = Field(default_factory=list)
    citations: list[CitationReference] = Field(default_factory=list)


def analysis_output_paths(input_path: Path, output_root: Path, preset: str) -> list[Path]:
    document_id = sanitize_filename(input_path.stem.lower(), "document")
    analysis_dir = output_root / "analysis"
    return [
        analysis_dir / f"{document_id}-{preset}.json",
        analysis_dir / f"{document_id}-{preset}.md",
    ]


def _bundle_root(output_root: Path) -> Path:
    return output_root / "llm"


def _source_sha256(input_path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with input_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_openai_client() -> object:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise DependencyMissingError("OPENAI_API_KEY is required for LLM analysis.")
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise DependencyMissingError("The `openai` package is required for LLM analysis.") from exc
    return OpenAI(api_key=api_key)


def _response_schema_for_preset(preset: str) -> type[BaseModel]:
    if preset == "summary":
        return SummaryAnalysis
    if preset == "entities":
        return EntitiesAnalysis
    if preset == "qa":
        return QAAnalysis
    raise ValidationError(f"Unsupported LLM preset '{preset}'.")


def _base_instructions(preset: str, *, question: str | None = None) -> str:
    if preset == "summary":
        return (
            "You are analyzing a PDF extraction bundle. Produce a concise executive summary, key points, risks, and action items. "
            "Only cite chunk IDs that appear in the provided context. Do not invent facts."
        )
    if preset == "entities":
        return (
            "You are extracting structured entities from a PDF extraction bundle. Return only grounded people, organizations, dates, "
            "amounts, and locations. Every entity must cite one or more chunk IDs from the provided context."
        )
    if preset == "qa":
        return (
            "Answer the user's question using only the provided chunk context. "
            f"Question: {question or ''}. "
            "Return an answer, a confidence level, follow-up questions, and citations using chunk IDs from the provided context."
        )
    raise ValidationError(f"Unsupported LLM preset '{preset}'.")


def _invoke_structured_response(
    *,
    model: str,
    schema: type[BaseModel],
    instructions: str,
    input_text: str,
) -> BaseModel:
    client = _require_openai_client()
    try:
        response = client.responses.parse(
            model=model,
            reasoning={"effort": "none"},
            text_format=schema,
            instructions=instructions,
            input=input_text,
        )
    except Exception as exc:  # pragma: no cover - network/runtime failure
        raise ProcessingFailureError(f"LLM analysis failed: {exc}") from exc

    parsed = getattr(response, "output_parsed", None)
    if parsed is None:
        raise ProcessingFailureError("LLM analysis returned no structured output.")
    return parsed


def _token_estimate_for_chunks(chunks: list[dict[str, object]]) -> int:
    return sum(int(chunk.get("estimated_tokens", 0)) for chunk in chunks)


def _render_chunk_context(chunks: list[dict[str, object]]) -> str:
    parts: list[str] = []
    for chunk in chunks:
        parts.append(
            "\n".join(
                [
                    f"Chunk ID: {chunk['chunk_id']}",
                    f"Heading: {chunk.get('heading') or 'Untitled'}",
                    f"Pages: {', '.join(str(page) for page in chunk.get('page_numbers', []))}",
                    str(chunk.get("retrieval_text") or chunk.get("text") or ""),
                ]
            ).strip()
        )
    return "\n\n---\n\n".join(part for part in parts if part).strip()


def _ensure_chunks_available(bundle: dict[str, object], *, preset: str) -> list[dict[str, object]]:
    chunks = [chunk for chunk in list(bundle.get("chunks", [])) if str(chunk.get("text") or chunk.get("retrieval_text") or "").strip()]
    if chunks:
        return chunks

    quality = dict(bundle.get("document", {}).get("extraction_quality", {}))
    ocr_recommended = bool(quality.get("ocr_recommended"))
    if ocr_recommended:
        raise ValidationError(
            f"No extractable text was found for LLM {preset} analysis. "
            "This document appears to be scan-heavy; run OCR first, then try again."
        )
    raise ValidationError(
        f"No extractable text was found for LLM {preset} analysis. "
        "Use a PDF with embedded text or run OCR first."
    )


def _section_chunk_groups(bundle: dict[str, object], *, token_limit: int) -> list[list[dict[str, object]]]:
    sections = list(bundle.get("sections", []))
    chunks = list(bundle.get("chunks", []))
    if not sections:
        return [chunks] if chunks else []

    section_groups: list[list[dict[str, object]]] = []
    current_group: list[dict[str, object]] = []
    current_tokens = 0

    for section in sections:
        section_pages = set(section.get("page_numbers", []))
        section_chunks = [
            chunk
            for chunk in chunks
            if section_pages.intersection(set(chunk.get("page_numbers", [])))
        ]
        if not section_chunks:
            continue
        section_tokens = _token_estimate_for_chunks(section_chunks)
        if current_group and current_tokens + section_tokens > token_limit:
            section_groups.append(current_group)
            current_group = []
            current_tokens = 0
        current_group.extend(section_chunks)
        current_tokens += section_tokens

    if current_group:
        section_groups.append(current_group)
    return section_groups


def _chunk_query_score(text: str, query_terms: Counter[str]) -> int:
    haystack = text.lower()
    return sum(haystack.count(term) * weight for term, weight in query_terms.items())


def _rank_chunks_for_question(bundle: dict[str, object], question: str) -> list[dict[str, object]]:
    terms = re.findall(r"[a-z0-9]{3,}", question.lower())
    counts = Counter(terms)
    chunks = list(bundle.get("chunks", []))
    if not counts:
        return chunks[:8]

    ranked = sorted(
        chunks,
        key=lambda chunk: (
            _chunk_query_score(str(chunk.get("retrieval_text") or chunk.get("text") or ""), counts),
            -int(chunk.get("estimated_tokens", 0)),
        ),
        reverse=True,
    )

    selected: list[dict[str, object]] = []
    token_total = 0
    for chunk in ranked:
        token_total += int(chunk.get("estimated_tokens", 0))
        selected.append(chunk)
        if token_total >= QA_CONTEXT_TOKEN_LIMIT:
            break
    return [chunk for chunk in selected if chunk] or chunks[:8]


def _resolve_citation_reference(reference: object, chunk_lookup: dict[str, dict[str, object]]) -> dict[str, object] | None:
    if isinstance(reference, str):
        chunk_id = reference
    elif isinstance(reference, dict):
        chunk_id = str(reference.get("chunk_id", "")).strip()
    else:
        return None
    if not chunk_id:
        return None

    chunk = chunk_lookup.get(chunk_id)
    if chunk is None:
        return {
            "chunk_id": chunk_id,
            "page_numbers": [],
            "heading": "",
            "available": False,
        }
    return {
        "chunk_id": chunk_id,
        "page_numbers": list(chunk.get("page_numbers", [])),
        "page_start": chunk.get("page_start"),
        "page_end": chunk.get("page_end"),
        "heading": chunk.get("heading"),
        "available": True,
    }


def _resolve_citations(value: object, chunk_lookup: dict[str, dict[str, object]]) -> object:
    if isinstance(value, dict):
        resolved: dict[str, object] = {}
        for key, item in value.items():
            if key == "citations" and isinstance(item, list):
                citations = [
                    citation
                    for citation in (_resolve_citation_reference(entry, chunk_lookup) for entry in item)
                    if citation is not None
                ]
                resolved[key] = citations
            else:
                resolved[key] = _resolve_citations(item, chunk_lookup)
        return resolved
    if isinstance(value, list):
        return [_resolve_citations(item, chunk_lookup) for item in value]
    return value


def _flatten_citations(value: object) -> list[dict[str, object]]:
    flattened: list[dict[str, object]] = []
    seen: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "chunk_id" in node and "page_numbers" in node:
                chunk_id = str(node["chunk_id"])
                if chunk_id not in seen:
                    seen.add(chunk_id)
                    flattened.append(node)
                return
            for item in node.values():
                walk(item)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(value)
    return flattened


def _render_markdown(
    *,
    input_path: Path,
    preset: str,
    model: str,
    question: str | None,
    strategy: str,
    resolved_result: dict[str, object],
) -> str:
    lines = [
        f"# LLM Analysis: {preset}",
        "",
        f"- Source: `{input_path.name}`",
        f"- Model: `{model}`",
        f"- Strategy: `{strategy}`",
    ]
    if question:
        lines.append(f"- Question: {question}")
    lines.append("")

    if preset == "summary":
        lines.append("## Executive Summary")
        lines.append("")
        lines.append(str(resolved_result.get("executive_summary", "")))
        for title, key in (("Key Points", "key_points"), ("Risks", "risks"), ("Action Items", "action_items")):
            items = resolved_result.get(key, [])
            if isinstance(items, list) and items:
                lines.extend(["", f"## {title}", ""])
                lines.extend([f"- {item}" for item in items])
        citations = resolved_result.get("citations", [])
        if isinstance(citations, list) and citations:
            lines.extend(["", "## Citations", ""])
            for citation in citations:
                lines.append(
                    f"- {citation['chunk_id']} (pages {', '.join(str(page) for page in citation.get('page_numbers', [])) or 'unknown'})"
                )
        return "\n".join(lines).strip() + "\n"

    if preset == "entities":
        for title, key in (
            ("People", "people"),
            ("Organizations", "organizations"),
            ("Dates", "dates"),
            ("Amounts", "amounts"),
            ("Locations", "locations"),
        ):
            items = resolved_result.get(key, [])
            if not isinstance(items, list) or not items:
                continue
            lines.extend(["", f"## {title}", ""])
            for item in items:
                pages = []
                if isinstance(item, dict):
                    pages = sorted({page for citation in item.get("citations", []) for page in citation.get("page_numbers", [])})
                suffix = f" (pages {', '.join(str(page) for page in pages)})" if pages else ""
                lines.append(f"- {item.get('value', '')}{suffix}")
        return "\n".join(lines).strip() + "\n"

    lines.extend(["## Answer", "", str(resolved_result.get("answer", "")), ""])
    lines.append(f"- Confidence: {resolved_result.get('confidence', 'unknown')}")
    follow_ups = resolved_result.get("follow_up_questions", [])
    if isinstance(follow_ups, list) and follow_ups:
        lines.extend(["", "## Follow-Up Questions", ""])
        lines.extend([f"- {item}" for item in follow_ups])
    citations = resolved_result.get("citations", [])
    if isinstance(citations, list) and citations:
        lines.extend(["", "## Citations", ""])
        for citation in citations:
            lines.append(
                f"- {citation['chunk_id']} (pages {', '.join(str(page) for page in citation.get('page_numbers', [])) or 'unknown'})"
            )
    return "\n".join(lines).strip() + "\n"


def _ensure_bundle(
    input_path: Path,
    output_root: Path,
    *,
    overwrite: bool,
) -> tuple[dict[str, object], Path, bool]:
    llm_dir = _bundle_root(output_root)
    json_path = llm_output_paths(input_path, llm_dir)[1]
    source_sha256 = _source_sha256(input_path)

    if json_path.exists():
        payload = load_llm_bundle(json_path)
        if payload.get("document", {}).get("source_sha256") == source_sha256:
            return payload, json_path, True
        if not overwrite:
            raise ValidationError(
                f"Existing LLM bundle for {input_path.name} does not match the current file. Use --overwrite to regenerate it."
            )

    extract_for_llm(input_path, llm_dir)
    return load_llm_bundle(json_path), json_path, False


def _run_summary_or_entities(
    bundle: dict[str, object],
    *,
    model: str,
    preset: str,
) -> tuple[BaseModel, str]:
    schema = _response_schema_for_preset(preset)
    chunks = _ensure_chunks_available(bundle, preset=preset)
    total_tokens = _token_estimate_for_chunks(chunks)
    instructions = _base_instructions(preset)

    if total_tokens <= SINGLE_PASS_TOKEN_LIMIT:
        parsed = _invoke_structured_response(
            model=model,
            schema=schema,
            instructions=instructions,
            input_text=_render_chunk_context(chunks),
        )
        return parsed, "single-pass"

    group_results: list[dict[str, object]] = []
    for group in _section_chunk_groups(bundle, token_limit=SECTION_GROUP_TOKEN_LIMIT):
        partial = _invoke_structured_response(
            model=model,
            schema=schema,
            instructions=instructions,
            input_text=_render_chunk_context(group),
        )
        group_results.append(partial.model_dump(mode="json"))

    final_input = "\n\n".join(
        [
            "Document metadata:",
            json.dumps(bundle.get("document", {}), indent=2),
            "Partial analyses:",
            json.dumps(group_results, indent=2),
            "Merge the partial analyses into one final grounded result. Reuse only cited chunk IDs from the partial analyses.",
        ]
    )
    final_result = _invoke_structured_response(
        model=model,
        schema=schema,
        instructions=instructions,
        input_text=final_input,
    )
    return final_result, "map-reduce"


def _run_qa(bundle: dict[str, object], *, model: str, question: str) -> tuple[BaseModel, str, list[dict[str, object]]]:
    _ensure_chunks_available(bundle, preset="qa")
    ranked_chunks = _rank_chunks_for_question(bundle, question)
    parsed = _invoke_structured_response(
        model=model,
        schema=QAAnalysis,
        instructions=_base_instructions("qa", question=question),
        input_text="\n\n".join(
            [
                "Document metadata:",
                json.dumps(bundle.get("document", {}), indent=2),
                "Relevant chunk context:",
                _render_chunk_context(ranked_chunks),
            ]
        ),
    )
    return parsed, "retrieval-qa", ranked_chunks


def analyze_pdf_with_llm(
    input_path: Path,
    output_root: Path,
    *,
    preset: str,
    question: str | None = None,
    model: str = DEFAULT_LLM_MODEL,
    overwrite_bundle: bool = False,
) -> dict[str, object]:
    normalized_preset = preset.strip().lower()
    if normalized_preset not in {"summary", "entities", "qa"}:
        raise ValidationError("preset must be one of: summary, entities, qa.")
    if normalized_preset == "qa" and not (question or "").strip():
        raise ValidationError("Question is required when preset is qa.")

    ensure_dir(output_root)
    bundle, bundle_path, bundle_reused = _ensure_bundle(input_path, output_root, overwrite=overwrite_bundle)

    if normalized_preset == "qa":
        parsed, strategy, context_chunks = _run_qa(bundle, model=model, question=(question or "").strip())
        analyzed_chunk_count = len(context_chunks)
    else:
        parsed, strategy = _run_summary_or_entities(bundle, model=model, preset=normalized_preset)
        analyzed_chunk_count = len(list(bundle.get("chunks", [])))

    chunk_lookup = {str(chunk["chunk_id"]): chunk for chunk in list(bundle.get("chunks", []))}
    parsed_payload = parsed.model_dump(mode="json")
    resolved_result = _resolve_citations(parsed_payload, chunk_lookup)
    flattened_citations = _flatten_citations(resolved_result)

    json_path, markdown_path = analysis_output_paths(input_path, output_root, normalized_preset)
    ensure_dir(json_path.parent)
    payload = {
        "format": "pdf-toolkit-llm-analysis-v1",
        "preset": normalized_preset,
        "model": model,
        "question": question.strip() if question else None,
        "strategy": strategy,
        "bundle_path": str(bundle_path),
        "bundle_reused": bundle_reused,
        "document": bundle.get("document", {}),
        "result": resolved_result,
        "citations": flattened_citations,
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    markdown_path.write_text(
        _render_markdown(
            input_path=input_path,
            preset=normalized_preset,
            model=model,
            question=question.strip() if question else None,
            strategy=strategy,
            resolved_result=resolved_result,
        ),
        encoding="utf-8",
    )

    return {
        "outputs": [json_path, markdown_path],
        "details": {
            "preset": normalized_preset,
            "model": model,
            "question": question.strip() if question else None,
            "strategy": strategy,
            "bundle_path": str(bundle_path),
            "bundle_reused": bundle_reused,
            "source_sha256": bundle.get("document", {}).get("source_sha256"),
            "analyzed_chunk_count": analyzed_chunk_count,
        },
    }


__all__ = ["DEFAULT_LLM_MODEL", "analysis_output_paths", "analyze_pdf_with_llm"]
