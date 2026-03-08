# Sample Workflows

## Merge monthly invoice PDFs

- Open `Start Here`
- Choose `Merge Invoice Packet`
- Add all invoice PDFs
- Keep or rename the suggested output file
- Run the job and review the merged preview

## Redact sensitive identifiers

- Open `Start Here`
- Choose `Redact PII For Sharing`
- Open the PDF
- Adjust the starter patterns if needed
- Export the redacted copy to a separate file

## Export tables for spreadsheets

- Open `Start Here`
- Choose `Export Tables To Spreadsheet Files`
- Select the report PDF
- Keep `All` or choose CSV, XLSX, or JSON
- Save the results into a clean output folder

## Build an LLM-ready extraction bundle

- Choose `Extract For LLM`
- Select the source PDF
- Save into a dedicated output folder
- Use the generated Markdown, JSON, and JSONL files for downstream prompts, retrieval, or indexing

## Analyze an extracted bundle with OpenAI

- Choose `Analyze With LLM`
- Point the app at a PDF and output folder
- Pick `Summary`, `Entities`, or `Q&A`
- Set `OPENAI_API_KEY` first, since the OpenAI analysis path is optional

## OCR scanned documents

- Open `Start Here`
- Choose `OCR Scanned Documents`
- Select the scanned PDF
- Save the searchable output copy
- If OCR tools are missing, keep using the rest of the app and enable OCR later

## Watch a folder for new PDFs

- Open `Start Here`
- Choose `Watch Incoming Folder`
- Point the workflow at an intake folder
- Confirm the default output folder and batch steps
- Run it as a repeatable folder workflow, or reuse the setup later from `Repeat Last Task`
