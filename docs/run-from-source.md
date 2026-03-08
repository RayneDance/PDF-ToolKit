# Run From Source

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e .[dev]
```

If you want optional OpenAI-powered LLM analysis from source, also install:

```powershell
python -m pip install -e .[llm]
```

## Start the app

```powershell
.\run_pdf_toolkit.bat
```

Or:

```powershell
python -m pdf_toolkit
```

When the app opens, use the `Start Here` screen to choose a workflow template or jump directly into combine, split, text export, tables, redaction, or folder automation.

## Build the packaged app

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_gui.ps1
```
