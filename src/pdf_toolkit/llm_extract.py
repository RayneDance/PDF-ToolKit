from __future__ import annotations

from math import ceil
from pathlib import Path
import hashlib
import json
import re

from pypdf import PdfReader

from pdf_toolkit.core import ensure_dir, extract_text_by_page, inspect_pdf, list_form_fields, sanitize_filename


def _estimate_tokens(text: str) -> int:
    stripped = text.strip()
    if not stripped:
        return 0
    return max(1, ceil(len(stripped) / 4))


def _source_sha256(input_path: Path) -> str:
    digest = hashlib.sha256()
    with input_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _split_text_with_overlap(text: str, *, chunk_size: int, overlap: int) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than 0.")
    if overlap < 0:
        raise ValueError("overlap cannot be negative.")
    if overlap >= chunk_size:
        raise ValueError("overlap must be smaller than chunk_size.")

    chunks: list[str] = []
    start = 0
    text_length = len(stripped)
    while start < text_length:
        end = min(start + chunk_size, text_length)
        if end < text_length:
            break_at = stripped.rfind("\n\n", start, end)
            if break_at <= start:
                break_at = stripped.rfind(" ", start, end)
            if break_at > start:
                end = break_at
        chunk = stripped[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= text_length:
            break
        start = max(end - overlap, start + 1)
    return chunks


def _paragraph_segments(text: str) -> list[str]:
    segments = [segment.strip() for segment in re.split(r"\n\s*\n", text) if segment.strip()]
    return segments or [text.strip()]


def _outline_entries(input_path: Path) -> list[dict[str, object]]:
    reader = PdfReader(str(input_path))
    try:
        outline = reader.outline
    except Exception:
        return []

    entries: list[dict[str, object]] = []
    seen: set[tuple[str, int]] = set()

    def walk(items: object) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, list):
                walk(item)
                continue
            title = getattr(item, "title", None)
            if not title:
                continue
            try:
                page_number = reader.get_destination_page_number(item) + 1
            except Exception:
                continue
            normalized = str(title).strip()
            key = (normalized, page_number)
            if not normalized or key in seen:
                continue
            seen.add(key)
            entries.append({"title": normalized, "page_number": page_number})

    walk(outline)
    entries.sort(key=lambda item: (int(item["page_number"]), str(item["title"]).lower()))
    return entries


def _page_headings(page_count: int, outlines: list[dict[str, object]]) -> dict[int, str]:
    if page_count <= 0:
        return {}
    if not outlines:
        return {page_number: f"Page {page_number}" for page_number in range(1, page_count + 1)}

    headings: dict[int, str] = {}
    active_index = 0
    active_heading = str(outlines[0]["title"])
    for page_number in range(1, page_count + 1):
        while active_index + 1 < len(outlines) and int(outlines[active_index + 1]["page_number"]) <= page_number:
            active_index += 1
            active_heading = str(outlines[active_index]["title"])
        headings[page_number] = active_heading
    return headings


def _reader_page_image_counts(input_path: Path) -> list[int]:
    reader = PdfReader(str(input_path))
    counts: list[int] = []
    for page in reader.pages:
        try:
            counts.append(len(page.images))
        except Exception:
            counts.append(0)
    return counts


def _sections_from_pages(
    document_id: str,
    page_texts: list[str],
    headings: dict[int, str],
) -> list[dict[str, object]]:
    sections: list[dict[str, object]] = []
    if not page_texts:
        return sections

    current_heading = headings.get(1, "Page 1")
    current_pages: list[int] = []
    current_texts: list[str] = []

    def flush() -> None:
        if not current_pages:
            return
        joined_text = "\n\n".join(text for text in current_texts if text).strip()
        section_id = f"{document_id}-s{len(sections) + 1:03d}"
        sections.append(
            {
                "section_id": section_id,
                "heading": current_heading,
                "page_start": current_pages[0],
                "page_end": current_pages[-1],
                "page_numbers": list(current_pages),
                "char_count": len(joined_text),
                "estimated_tokens": _estimate_tokens(joined_text),
                "text": joined_text,
            }
        )

    for page_number, page_text in enumerate(page_texts, start=1):
        heading = headings.get(page_number, f"Page {page_number}")
        if current_pages and heading != current_heading:
            flush()
            current_pages = []
            current_texts = []
            current_heading = heading
        elif not current_pages:
            current_heading = heading
        current_pages.append(page_number)
        current_texts.append(page_text.strip())
    flush()
    return sections


def _segment_document(
    page_texts: list[str],
    headings: dict[int, str],
    *,
    chunk_size: int,
    overlap: int,
) -> list[dict[str, object]]:
    max_segment_chars = max(200, chunk_size // 2)
    segment_overlap = min(overlap, max(50, max_segment_chars // 4))
    segments: list[dict[str, object]] = []

    for page_number, page_text in enumerate(page_texts, start=1):
        clean_text = page_text.strip()
        if not clean_text:
            continue
        heading = headings.get(page_number, f"Page {page_number}")
        for paragraph in _paragraph_segments(clean_text):
            paragraph_chunks = (
                _split_text_with_overlap(paragraph, chunk_size=max_segment_chars, overlap=segment_overlap)
                if len(paragraph) > max_segment_chars
                else [paragraph]
            )
            for segment_text in paragraph_chunks:
                segments.append(
                    {
                        "page_number": page_number,
                        "heading": heading,
                        "text": segment_text.strip(),
                    }
                )
    return segments


def _compose_chunk_text(segments: list[dict[str, object]], *, include_page_markers: bool) -> str:
    lines: list[str] = []
    last_page: int | None = None
    for segment in segments:
        page_number = int(segment["page_number"])
        if include_page_markers and page_number != last_page:
            lines.append(f"Page {page_number}")
            last_page = page_number
        lines.append(str(segment["text"]))
    return "\n\n".join(line for line in lines if line).strip()


def _build_chunks(
    document_id: str,
    segments: list[dict[str, object]],
    *,
    chunk_size: int,
    overlap: int,
    include_page_markers: bool,
) -> list[dict[str, object]]:
    if not segments:
        return []

    chunks: list[dict[str, object]] = []
    index = 0
    while index < len(segments):
        start_index = index
        current_segments: list[dict[str, object]] = []
        current_chars = 0

        while index < len(segments):
            segment_text = str(segments[index]["text"]).strip()
            if not segment_text:
                index += 1
                continue
            if current_segments and int(segments[index]["page_number"]) != int(current_segments[0]["page_number"]):
                break
            extra_chars = len(segment_text) if not current_segments else len(segment_text) + 2
            if current_segments and current_chars + extra_chars > chunk_size:
                break
            current_segments.append(segments[index])
            current_chars += extra_chars
            index += 1

        if not current_segments:
            index += 1
            continue

        page_numbers = sorted({int(segment["page_number"]) for segment in current_segments})
        first_heading = str(current_segments[0]["heading"])
        chunk_text = _compose_chunk_text(current_segments, include_page_markers=include_page_markers)
        retrieval_text = f"Section: {first_heading}\n{chunk_text}".strip()
        chunk_id = f"{document_id}-c{len(chunks) + 1:03d}"
        chunks.append(
            {
                "chunk_id": chunk_id,
                "document_id": document_id,
                "page_numbers": page_numbers,
                "page_start": page_numbers[0],
                "page_end": page_numbers[-1],
                "chunk_index": len(chunks) + 1,
                "heading": first_heading,
                "char_count": len(chunk_text),
                "estimated_tokens": _estimate_tokens(chunk_text),
                "text": chunk_text,
                "retrieval_text": retrieval_text,
                "citations": [
                    {
                        "page_number": page_number,
                        "label": f"Page {page_number}",
                        "heading": first_heading,
                    }
                    for page_number in page_numbers
                ],
            }
        )

        if index >= len(segments) or overlap <= 0:
            continue

        overlap_chars = 0
        restart_index = start_index + len(current_segments) - 1
        for offset, segment in enumerate(reversed(current_segments), start=1):
            overlap_chars += len(str(segment["text"]))
            restart_index = start_index + len(current_segments) - offset
            if overlap_chars >= overlap:
                break

        index = max(restart_index, start_index + 1)

    return chunks


def _build_markdown(
    input_path: Path,
    page_texts: list[str],
    *,
    include_page_markers: bool,
    metadata: dict[str, object],
    sections: list[dict[str, object]],
) -> str:
    lines = [
        "# LLM Extraction Bundle",
        "",
        f"- Source: `{input_path.name}`",
        f"- Pages: {metadata['page_count']}",
        f"- Document ID: `{metadata['document_id']}`",
        f"- Source SHA256: `{metadata['source_sha256']}`",
    ]
    extraction_quality = metadata.get("extraction_quality", {})
    if metadata.get("metadata"):
        lines.append(f"- Metadata keys: {', '.join(sorted(metadata['metadata']))}")
    if extraction_quality:
        lines.append(
            "- Extraction quality: "
            f"OCR recommended={bool(extraction_quality.get('ocr_recommended'))}, "
            f"empty pages={len(extraction_quality.get('empty_pages', []))}, "
            f"image-only pages={len(extraction_quality.get('image_only_pages', []))}"
        )
    lines.extend(["", "## Sections", ""])

    for section in sections:
        lines.append(f"- {section['heading']} (pages {section['page_start']}-{section['page_end']})")
    lines.append("")

    for page_number, text in enumerate(page_texts, start=1):
        page_body = text.strip() or "[No extractable text]"
        if include_page_markers:
            lines.append(f"## Page {page_number}")
            lines.append("")
        lines.append(page_body)
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def llm_output_paths(input_path: Path, output_dir: Path) -> list[Path]:
    document_id = sanitize_filename(input_path.stem.lower(), "document")
    return [
        output_dir / f"{document_id}-llm.md",
        output_dir / f"{document_id}-llm.json",
        output_dir / f"{document_id}-llm.jsonl",
    ]


def load_llm_bundle(json_path: Path) -> dict[str, object]:
    return json.loads(json_path.read_text(encoding="utf-8"))


def extract_for_llm(
    input_path: Path,
    output_dir: Path,
    *,
    chunk_size: int = 1200,
    overlap: int = 200,
    include_page_markers: bool = True,
    include_metadata: bool = True,
) -> dict[str, object]:
    ensure_dir(output_dir)

    info = inspect_pdf(input_path)
    page_texts = extract_text_by_page(input_path)
    document_id = sanitize_filename(input_path.stem.lower(), "document")
    source_sha256 = _source_sha256(input_path)
    outlines = _outline_entries(input_path) if include_metadata else []
    headings = _page_headings(info.page_count, outlines)
    page_image_counts = _reader_page_image_counts(input_path)
    empty_pages = [page_number for page_number, text in enumerate(page_texts, start=1) if not text.strip()]
    image_only_pages = [
        page_number
        for page_number, text in enumerate(page_texts, start=1)
        if not text.strip() and page_image_counts[page_number - 1] > 0
    ]

    extraction_quality = {
        "ocr_recommended": bool(image_only_pages),
        "empty_pages": empty_pages,
        "image_only_pages": image_only_pages,
    }

    bundle_metadata: dict[str, object] = {
        "document_id": document_id,
        "source_path": str(input_path),
        "source_sha256": source_sha256,
        "page_count": info.page_count,
        "attachment_count": info.attachment_count,
        "form_field_count": info.form_field_count,
        "metadata": info.metadata if include_metadata else {},
        "bookmarks": [entry["title"] for entry in outlines] if include_metadata else [],
        "form_fields": [
            {"name": field.name, "field_type": field.field_type, "value": field.value}
            for field in list_form_fields(input_path)
        ]
        if include_metadata
        else [],
        "extraction_quality": extraction_quality,
    }

    sections = _sections_from_pages(document_id, page_texts, headings)
    segments = _segment_document(page_texts, headings, chunk_size=chunk_size, overlap=overlap)
    chunks = _build_chunks(
        document_id,
        segments,
        chunk_size=chunk_size,
        overlap=overlap,
        include_page_markers=include_page_markers,
    )

    pages: list[dict[str, object]] = []
    for page_number, page_text in enumerate(page_texts, start=1):
        clean_text = page_text.strip()
        pages.append(
            {
                "page_number": page_number,
                "heading": headings.get(page_number, f"Page {page_number}"),
                "char_count": len(clean_text),
                "estimated_tokens": _estimate_tokens(clean_text),
                "has_text": bool(clean_text),
                "text": clean_text,
                "chunk_count": sum(1 for chunk in chunks if page_number in chunk["page_numbers"]),
            }
        )

    markdown_path, json_path, jsonl_path = llm_output_paths(input_path, output_dir)

    markdown_path.write_text(
        _build_markdown(
            input_path,
            page_texts,
            include_page_markers=include_page_markers,
            metadata=bundle_metadata,
            sections=sections,
        ),
        encoding="utf-8",
    )

    payload = {
        "format": "pdf-toolkit-llm-bundle-v1",
        "document": bundle_metadata,
        "pages": pages,
        "sections": sections,
        "chunks": chunks,
        "settings": {
            "chunk_size": chunk_size,
            "overlap": overlap,
            "include_page_markers": include_page_markers,
            "include_metadata": include_metadata,
        },
    }
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with jsonl_path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk) + "\n")

    return {
        "outputs": [markdown_path, json_path, jsonl_path],
        "details": {
            "document_id": document_id,
            "page_count": info.page_count,
            "chunk_count": len(chunks),
            "section_count": len(sections),
            "output_dir": str(output_dir),
            "chunk_size": chunk_size,
            "overlap": overlap,
            "source_sha256": source_sha256,
            "ocr_recommended": extraction_quality["ocr_recommended"],
        },
    }


__all__ = ["extract_for_llm", "llm_output_paths", "load_llm_bundle"]
