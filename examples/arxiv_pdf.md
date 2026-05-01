# arXiv papers — PDF text extraction

A 4-component pipeline that downloads two famous ML papers from arXiv,
extracts their full text via pdfplumber, computes word + character
counts, writes a per-paper summary CSV.

## Pipeline

```
csv_file_ingestion → pdf_text_extractor → formula → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `csv_file_ingestion` | ingestion | Load a 2-row manifest of `paper, path` |
| 2 | `pdf_text_extractor` | transformation | Extract text from each PDF with pdfplumber |
| 3 | `formula` | transformation | Compute `char_count` + `word_count`; drop the giant text column |
| 4 | `dataframe_to_csv` | sink | Per-paper summary |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_arxiv_pdf_demo.sh | bash
cd arxiv-pdf-demo
uv run dg launch --assets '*'
```

The setup script pre-downloads two papers via curl (arXiv is fine with
plain `python-requests`-style fetches when you go through their PDF
endpoints); the dagster pipeline then operates on local file paths.

## Output

`/tmp/arxiv_summary.csv`:

```
paper,path,char_count,word_count
Attention Is All You Need,           /tmp/arxiv_papers/...pdf,  35525,  2033
Pre-train Prompt and Predict,        /tmp/arxiv_papers/...pdf, 264609, 12210
```

## What this demo shows

- **First document-extraction demo.** `pdf_text_extractor` accepts file
  paths or raw bytes; pdfplumber handles layout and text decoding.
- **Manifest pattern.** The CSV manifest decouples "which PDFs to
  process" from "how to extract them." Add a row, re-materialize, get
  another paper's stats.
- **Drop the huge column before the sink.** `formula`'s
  `drop_source_columns` field removes `text` after computing aggregates,
  so the output CSV stays small.

## Extending

Add `regex_parser` (`mode: extract`) downstream to pull abstract
sections, citation patterns, or references. Combine with
`text_preprocessing` (lowercase, strip stopwords) for downstream NLP.
