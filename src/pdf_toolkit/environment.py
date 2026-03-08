from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
import os
import shutil
import sys


@dataclass(slots=True)
class DependencyStatus:
    name: str
    category: str
    available: bool
    detail: str
    remediation: str
    required: bool


def _app_search_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).resolve()
        roots.append(executable.parent)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass).resolve())
    else:
        roots.append(Path(__file__).resolve().parents[2])
    return roots


def _command_candidates(name: str, aliases: tuple[str, ...] = ()) -> list[str]:
    candidates = [name, *aliases]
    if sys.platform.startswith("win"):
        expanded: list[str] = []
        for candidate in candidates:
            expanded.append(candidate)
            if not candidate.lower().endswith(".exe"):
                expanded.append(f"{candidate}.exe")
        return expanded
    return candidates


def resolve_command_path(name: str, aliases: tuple[str, ...] = ()) -> str | None:
    for candidate in _command_candidates(name, aliases):
        resolved = shutil.which(candidate)
        if resolved is not None:
            return resolved

    search_dirs = (
        "vendor",
        "vendor/bin",
        "vendor/tools",
        "tools",
        "bin",
    )
    for root in _app_search_roots():
        for relative_dir in search_dirs:
            base_dir = root / relative_dir
            if not base_dir.exists():
                continue
            for candidate in _command_candidates(name, aliases):
                target = base_dir / candidate
                if target.exists():
                    return str(target)
    return None


def _check_python_module(name: str, remediation: str, *, required: bool = True) -> DependencyStatus:
    available = find_spec(name) is not None
    return DependencyStatus(
        name=name,
        category="python",
        available=available,
        detail="installed" if available else "missing",
        remediation=remediation,
        required=required,
    )


def _check_command(name: str, remediation: str, aliases: tuple[str, ...] = (), *, required: bool = True) -> DependencyStatus:
    resolved = resolve_command_path(name, aliases)
    available = resolved is not None
    return DependencyStatus(
        name=name,
        category="command",
        available=available,
        detail=f"resolved to {resolved}" if available else "not found on PATH or in bundled tools",
        remediation=remediation,
        required=required,
    )


def _check_environment_variable(name: str, remediation: str, *, required: bool = True) -> DependencyStatus:
    value = os.getenv(name, "").strip()
    available = bool(value)
    return DependencyStatus(
        name=name,
        category="environment",
        available=available,
        detail="set" if available else "missing",
        remediation=remediation,
        required=required,
    )


def collect_doctor_status(feature_set: str = "all") -> list[DependencyStatus]:
    statuses = [
        _check_python_module("pypdf", "Install with `python -m pip install pypdf`."),
        _check_python_module("pdfplumber", "Install with `python -m pip install pdfplumber`."),
        _check_python_module("pypdfium2", "Install with `python -m pip install pypdfium2`."),
        _check_python_module("PIL", "Install with `python -m pip install Pillow`."),
    ]

    if feature_set in {"all", "redaction"}:
        statuses.append(_check_python_module("fitz", "Install with `python -m pip install PyMuPDF`."))
    if feature_set in {"all", "tables", "batch"}:
        statuses.append(_check_python_module("yaml", "Install with `python -m pip install PyYAML`."))
        statuses.append(_check_python_module("openpyxl", "Install with `python -m pip install openpyxl`."))
    if feature_set in {"all", "batch"}:
        statuses.append(_check_python_module("watchdog", "Install with `python -m pip install watchdog`."))
    if feature_set in {"all", "ocr"}:
        statuses.extend(
            [
                _check_command("ocrmypdf", "Install OCRmyPDF or bundle it in `vendor/bin`.", required=False),
                _check_command("tesseract", "Install Tesseract OCR or bundle it in `vendor/bin`.", required=False),
                _check_command(
                    "gswin64c",
                    "Install Ghostscript or bundle `gswin64c.exe` in `vendor/bin`.",
                    aliases=("gswin32c", "gs"),
                    required=False,
                ),
            ]
        )
    if feature_set in {"all", "render"}:
        statuses.append(_check_command("pdftoppm", "Install Poppler or bundle it in `vendor/bin`.", required=False))
    if feature_set in {"all", "llm"}:
        statuses.extend(
            [
                _check_python_module("openai", "Install with `python -m pip install openai`.", required=False),
                _check_python_module("pydantic", "Install with `python -m pip install pydantic`.", required=False),
                _check_environment_variable("OPENAI_API_KEY", "Set OPENAI_API_KEY to enable OpenAI-powered analysis.", required=False),
            ]
        )
    return statuses


def ensure_command_available(name: str, remediation: str, aliases: tuple[str, ...] = ()) -> str:
    resolved = resolve_command_path(name, aliases)
    if resolved is None:
        from pdf_toolkit.errors import DependencyMissingError

        raise DependencyMissingError(f"{name} is required. {remediation}")
    return resolved
