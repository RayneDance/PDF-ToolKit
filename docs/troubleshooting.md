# Troubleshooting

## The packaged app will not start

- Re-extract the ZIP to a normal folder.
- Make sure Windows did not block the download in file properties.
- Run the app from a writable location such as `Documents`.

## OCR shows as missing

That is expected unless OCR tools were installed separately or bundled into the app.

## LLM analysis shows as unavailable

That is expected unless `OPENAI_API_KEY` is configured. Local LLM-ready extraction can still be used without it.

## Preview does not update

- Make sure the selected input is a PDF file.
- Try another operation, then return to the current one.
- Use the Diagnostics tab to confirm core dependencies are available.

## Source install issues

- Confirm you are using Python 3.11 or newer.
- Recreate `.venv` and reinstall with `python -m pip install -e .[dev]`.
- If you want OpenAI-powered analysis from source, also install `python -m pip install -e .[llm]`.

## Need more detail

Open a GitHub issue and include:

- app version
- install type
- Windows version
- exact steps
- any sample files that reproduce the problem
