# Enable OpenAI Analysis

LLM-ready extraction is built into PDF Toolkit. OpenAI-powered analysis is optional.

## What works without OpenAI

- `Extract For LLM`
- local Markdown, JSON, and JSONL bundle generation
- chunked document output for downstream prompts, retrieval, and indexing

## What requires OpenAI

- `Analyze With LLM`
- summary, entity extraction, and Q&A analysis modes

## Windows packaged app

If you are using the packaged Windows app, set `OPENAI_API_KEY` in your environment before launching the app if you want OpenAI-powered analysis.

## Source install

Install the optional extras:

```powershell
python -m pip install -e .[llm]
```

Then set your API key:

```powershell
$env:OPENAI_API_KEY="your-key-here"
```

## Notes

- OpenAI analysis is optional by design.
- Missing `OPENAI_API_KEY` should not block the rest of the toolkit.
- Scan-heavy PDFs with no extractable text should be OCRed first before running analysis.
