from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from pdf_toolkit.application import execute_job_or_raise, prepare_request
from pdf_toolkit.errors import PdfToolkitError

app = typer.Typer(
    help="An enterprise-oriented CLI for practical PDF workflows.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _exit_error(exc: Exception) -> None:
    code = exc.exit_code if isinstance(exc, PdfToolkitError) else 4
    console.print(f"[bold red]Error:[/bold red] {exc}")
    raise typer.Exit(code=code)


def _print_outputs(outputs: list[Path], quiet: bool) -> None:
    if quiet:
        return
    for output in outputs:
        console.print(f"[green]Wrote[/green] {output}")


def _run_cli(command: str, values: dict[str, object], *, report: Path | None = None, overwrite: bool = False, quiet: bool = False) -> dict[str, object]:
    try:
        request = prepare_request(command, values, report_path=report, overwrite=overwrite)
        result = execute_job_or_raise(request)
    except Exception as exc:
        _exit_error(exc)
        return {}
    _print_outputs(result.outputs, quiet)
    return result.details


def _render_table(title: str, columns: list[str], rows: list[list[str]]) -> None:
    table = Table(title=title)
    for column in columns:
        table.add_column(column)
    for row in rows:
        table.add_row(*row)
    console.print(table)


@app.command("doctor")
def doctor_command(feature: str = typer.Option("all", help="Dependency set to validate: all, ocr, redaction, tables, batch, render, llm.")) -> None:
    details = _run_cli("doctor", {"feature": feature})
    rows = []
    missing = False
    for status in details.get("statuses", []):
        available = "ok" if status["available"] else "missing"
        if not status["available"]:
            missing = True
        rows.append([status["name"], status["category"], available, status.get("remediation", "") if not status["available"] else ""])
    _render_table(f"Dependency Check: {details.get('feature', feature)}", ["Name", "Type", "Status", "Remediation"], rows)
    if missing:
        raise typer.Exit(code=3)


@app.command("scan-detect")
def scan_detect_command(input_path: Path) -> None:
    details = _run_cli("scan-detect", {"input_path": input_path})
    rows = [[str(page["page_number"]), str(page["mode"]), str(page["text_characters"]), str(page["image_count"])] for page in details["pages"]]
    _render_table(f"Scan Detection: {input_path.name}", ["Page", "Mode", "Text chars", "Images"], rows)
    console.print(f"Summary: [cyan]{details['summary']}[/cyan]")


@app.command("inspect")
def inspect_command(input_path: Path) -> None:
    details = _run_cli("inspect", {"input_path": input_path})
    rows = [
        ["Path", str(details["path"])],
        ["Pages", str(details["page_count"])],
        ["Encrypted", "yes" if details["is_encrypted"] else "no"],
        ["Attachments", str(details["attachment_count"])],
        ["Form fields", str(details["form_field_count"])],
    ]
    rows.extend([[f"Metadata: {key}", str(value)] for key, value in sorted(details.get("metadata", {}).items())])
    rows.extend([[f"Page {index} size", f"{width:.1f} x {height:.1f} pt"] for index, (width, height) in enumerate(details.get("page_sizes", []), start=1)])
    _render_table(f"PDF Summary: {input_path.name}", ["Field", "Value"], rows)


def _quiet_write(command: str, values: dict[str, object], *, report: Path | None, overwrite: bool, quiet: bool) -> None:
    details = _run_cli(command, values, report=report, overwrite=overwrite, quiet=quiet)
    if command == "extract-text" and not values.get("output") and not quiet and "text" in details:
        console.print(details["text"])


@app.command("merge")
def merge_command(inputs: list[Path] = typer.Argument(...), output: Path = typer.Option(..., "--output", "-o"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("merge", {"inputs": inputs, "output": output}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("split")
def split_command(input_path: Path, output_dir: Path = typer.Option(..., "--output", "-o"), ranges: str | None = typer.Option(None), every_page: bool = typer.Option(False), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("split", {"input_path": input_path, "output_dir": output_dir, "ranges": ranges, "every_page": every_page}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("select")
def select_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), pages: str = typer.Option(...), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("select", {"input_path": input_path, "output": output, "pages": pages}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("rotate")
def rotate_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), degrees: int = typer.Option(...), pages: str | None = typer.Option(None), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("rotate", {"input_path": input_path, "output": output, "degrees": degrees, "pages": pages}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("extract-text")
def extract_text_command(input_path: Path, output: Path | None = typer.Option(None, "--output", "-o"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("extract-text", {"input_path": input_path, "output": output}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("extract-llm")
def extract_llm_command(
    input_path: Path,
    output_dir: Path = typer.Option(..., "--output-dir", "-o"),
    chunk_size: int = typer.Option(1200, "--chunk-size"),
    overlap: int = typer.Option(200, "--overlap"),
    include_page_markers: bool = typer.Option(True, "--include-page-markers/--no-page-markers"),
    include_metadata: bool = typer.Option(True, "--include-metadata/--no-metadata"),
    report: Path | None = typer.Option(None, "--report"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    _quiet_write(
        "extract-llm",
        {
            "input_path": input_path,
            "output_dir": output_dir,
            "chunk_size": chunk_size,
            "overlap": overlap,
            "include_page_markers": include_page_markers,
            "include_metadata": include_metadata,
        },
        report=report,
        overwrite=overwrite,
        quiet=quiet,
    )


@app.command("analyze-llm")
def analyze_llm_command(
    input_path: Path,
    output_dir: Path = typer.Option(..., "--output-dir", "-o"),
    preset: str = typer.Option("summary", "--preset"),
    question: str | None = typer.Option(None, "--question"),
    model: str = typer.Option("gpt-5-mini", "--model"),
    report: Path | None = typer.Option(None, "--report"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    _quiet_write(
        "analyze-llm",
        {
            "input_path": input_path,
            "output_dir": output_dir,
            "preset": preset,
            "question": question,
            "model": model,
        },
        report=report,
        overwrite=overwrite,
        quiet=quiet,
    )


@app.command("protect")
def protect_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), password: str = typer.Option(..., prompt=True, hide_input=True, confirmation_prompt=True), owner_password: str | None = typer.Option(None), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("protect", {"input_path": input_path, "output": output, "password": password, "owner_password": owner_password}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("unlock")
def unlock_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), password: str = typer.Option(..., prompt=True, hide_input=True), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("unlock", {"input_path": input_path, "output": output, "password": password}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("stamp-text")
def stamp_text_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), text: str = typer.Option(...), pages: str | None = typer.Option(None), font_size: int = typer.Option(48), opacity: float = typer.Option(0.2), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("stamp-text", {"input_path": input_path, "output": output, "text": text, "pages": pages, "font_size": font_size, "opacity": opacity}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("set-metadata")
def set_metadata_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), values: list[str] = typer.Option(..., "--value"), clear_existing: bool = typer.Option(False), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("set-metadata", {"input_path": input_path, "output": output, "values": values, "clear_existing": clear_existing}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("clear-metadata")
def clear_metadata_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("clear-metadata", {"input_path": input_path, "output": output}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("compress")
def compress_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("compress", {"input_path": input_path, "output": output}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("number-pages")
def number_pages_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), pages: str | None = typer.Option(None), format_text: str = typer.Option("Page {page} of {total}"), start_number: int = typer.Option(1), position: str = typer.Option("bottom-right"), margin: float = typer.Option(36.0), font_size: int = typer.Option(10), opacity: float = typer.Option(0.85), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("number-pages", {"input_path": input_path, "output": output, "pages": pages, "format_text": format_text, "start_number": start_number, "position": position, "margin": margin, "font_size": font_size, "opacity": opacity}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("crop")
def crop_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), pages: str | None = typer.Option(None), left: float = typer.Option(0.0), right: float = typer.Option(0.0), top: float = typer.Option(0.0), bottom: float = typer.Option(0.0), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("crop", {"input_path": input_path, "output": output, "pages": pages, "left": left, "right": right, "top": top, "bottom": bottom}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("render")
def render_command(input_path: Path, output_dir: Path = typer.Option(..., "--output", "-o"), pages: str | None = typer.Option(None), dpi: int = typer.Option(150), image_format: str = typer.Option("png"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("render", {"input_path": input_path, "output_dir": output_dir, "pages": pages, "dpi": dpi, "image_format": image_format}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("extract-images")
def extract_images_command(input_path: Path, output_dir: Path = typer.Option(..., "--output", "-o"), pages: str | None = typer.Option(None), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("extract-images", {"input_path": input_path, "output_dir": output_dir, "pages": pages}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("images-to-pdf")
def images_to_pdf_command(inputs: list[Path] = typer.Argument(...), output: Path = typer.Option(..., "--output", "-o"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("images-to-pdf", {"inputs": inputs, "output": output}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("attachments-list")
def attachments_list_command(input_path: Path) -> None:
    details = _run_cli("attachments-list", {"input_path": input_path})
    attachments = details["attachments"]
    if not attachments:
        console.print("[yellow]No attachments found.[/yellow]")
        return
    _render_table(f"Attachments: {input_path.name}", ["Name", "Size", "Description"], [[item["name"], str(item["size"]), str(item.get("description") or "")] for item in attachments])


@app.command("attachments-add")
def attachments_add_command(input_path: Path, attachments: list[Path] = typer.Argument(...), output: Path = typer.Option(..., "--output", "-o"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("attachments-add", {"input_path": input_path, "attachments": attachments, "output": output}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("attachments-extract")
def attachments_extract_command(input_path: Path, output_dir: Path = typer.Option(..., "--output", "-o"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("attachments-extract", {"input_path": input_path, "output_dir": output_dir}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("form-fields")
def form_fields_command(input_path: Path) -> None:
    details = _run_cli("form-fields", {"input_path": input_path})
    fields = details["fields"]
    if not fields:
        console.print("[yellow]No form fields found.[/yellow]")
        return
    _render_table(f"Form Fields: {input_path.name}", ["Name", "Type", "Value"], [[item["name"], item["field_type"], str(item["value"])] for item in fields])


@app.command("fill-form")
def fill_form_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), values: list[str] = typer.Option(..., "--value"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("fill-form", {"input_path": input_path, "output": output, "values": values}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("bookmarks")
def bookmarks_command(input_path: Path) -> None:
    details = _run_cli("bookmarks", {"input_path": input_path})
    bookmarks = details["bookmarks"]
    if not bookmarks:
        console.print("[yellow]No bookmarks found.[/yellow]")
        return
    _render_table(f"Bookmarks: {input_path.name}", ["Title"], [[item] for item in bookmarks])


@app.command("remove-annotations")
def remove_annotations_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), pages: str | None = typer.Option(None), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("remove-annotations", {"input_path": input_path, "output": output, "pages": pages}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("ocr")
def ocr_command(input_path: Path, output: Path = typer.Option(..., "--output", "-o"), language: str | None = typer.Option(None), skip_existing_text: bool = typer.Option(False, "--skip-existing-text"), text_output: Path | None = typer.Option(None, "--text-output"), json_output: Path | None = typer.Option(None, "--json-output"), force: bool = typer.Option(False, "--force"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("ocr", {"input_path": input_path, "output": output, "language": language, "skip_existing_text": skip_existing_text, "text_output": text_output, "json_output": json_output, "force": force}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("redact")
def redact_command(input_path: Path, output: Path | None = typer.Option(None, "--output", "-o"), pattern: list[str] = typer.Option([], "--pattern"), regex: bool = typer.Option(False, "--regex"), case_sensitive: bool = typer.Option(False, "--case-sensitive"), pages: str | None = typer.Option(None, "--pages"), box: list[str] = typer.Option([], "--box"), label: str | None = typer.Option(None, "--label"), dry_run: bool = typer.Option(False, "--dry-run"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("redact", {"input_path": input_path, "output": output, "pattern": pattern, "regex": regex, "case_sensitive": case_sensitive, "pages": pages, "box": box, "label": label, "dry_run": dry_run}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("tables-extract")
def tables_extract_command(input_path: Path, output_dir: Path = typer.Option(..., "--output-dir", "-o"), pages: str | None = typer.Option(None, "--pages"), format_name: str = typer.Option("csv", "--format"), ocr_first: bool = typer.Option(False, "--ocr-first"), report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("tables-extract", {"input_path": input_path, "output_dir": output_dir, "pages": pages, "format_name": format_name, "ocr_first": ocr_first}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("batch-run")
def batch_run_command(manifest_path: Path, report: Path | None = typer.Option(None, "--report"), overwrite: bool = typer.Option(False, "--overwrite"), quiet: bool = typer.Option(False, "--quiet")) -> None:
    _quiet_write("batch-run", {"manifest_path": manifest_path}, report=report, overwrite=overwrite, quiet=quiet)


@app.command("watch-folder")
def watch_folder_command(input_dir: Path, manifest_path: Path, once: bool = typer.Option(False, "--once"), recursive: bool = typer.Option(False, "--recursive"), overwrite: bool = typer.Option(False, "--overwrite")) -> None:
    _run_cli("watch-folder", {"input_dir": input_dir, "manifest_path": manifest_path, "once": once, "recursive": recursive}, overwrite=overwrite)


@app.command("deduplicate-folder")
def deduplicate_folder_command(
    input_dir: Path,
    recursive: bool = typer.Option(True, "--recursive/--no-recursive"),
    delete_duplicates: bool = typer.Option(False, "--delete-duplicates"),
    report: Path | None = typer.Option(None, "--report"),
    overwrite: bool = typer.Option(False, "--overwrite"),
    quiet: bool = typer.Option(False, "--quiet"),
) -> None:
    details = _run_cli(
        "deduplicate-folder",
        {"input_dir": input_dir, "recursive": recursive, "delete_duplicates": delete_duplicates},
        report=report,
        overwrite=overwrite,
        quiet=quiet,
    )
    rows = []
    for group in details.get("groups", []):
        duplicates = ", ".join(group["duplicate_files"]) if group["duplicate_files"] else ""
        rows.append([group["kept_file"], duplicates, str(group["file_size"])])
    if rows:
        _render_table("Duplicate PDFs", ["Kept File", "Duplicate Files", "Bytes"], rows)
    elif not quiet:
        console.print("[green]No duplicate PDFs found.[/green]")
    if not quiet:
        console.print(
            f"Scanned {details.get('scanned_file_count', 0)} PDF(s), "
            f"found {details.get('duplicate_file_count', 0)} duplicate file(s), "
            f"removed {details.get('removed_count', 0)}."
        )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
