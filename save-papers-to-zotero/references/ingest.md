# Ingest from Identifier Lists or BibTeX

Use `scripts/zotero_ingest.py` when you have a plain list of DOIs / arXiv IDs or a `.bib` file instead of prepared Zotero items. The script resolves metadata from public APIs (Crossref, arXiv) or parses BibTeX locally, and **writes a batch manifest** that `scripts/zotero_batch_import.py` then imports. It does not contact Zotero and does not write to any library.

This is a two-step, reviewable flow:

```text
ids.txt / refs.bib -> zotero_ingest.py -> manifest.json (review / batch --dry-run)
                                                       -> zotero_batch_import.py
```

## Why a separate resolver step

The batch importer requires a translator-style item plus a PDF for every paper. Building those by hand for a long identifier list is error-prone. The resolver produces dependable metadata from authoritative sources, reports per-identifier failures without dropping them silently, and emits a plain JSON manifest you can inspect before anything is written to Zotero.

## CLI

```text
python "<skill-dir>/scripts/zotero_ingest.py" \
  (--identifiers <ids.txt|-> | --bibtex <refs.bib>) \
  --collection "<exact collection name>" \
  [--out <manifest.json>] \
  [--report <ingest-report.json>] \
  [--tag <t> ...] [--note <n> ...] \
  [--reading-status to-read|reading|none] \
  [--priority high|medium|low] \
  [--mailto <email>] \
  [--delay <sec>] \
  [--http-timeout <sec>]
```

- `--identifiers` and `--bibtex` are mutually exclusive and one is required. `--identifiers -` reads from stdin.
- `--collection` is required and is baked into the manifest top level. The resolver does not contact Zotero and does not create the collection; it must already exist in Zotero before the later batch import (the local server cannot create collections through its API).
- `--out` defaults to `<input-stem>.manifest.json` next to the input; stdin input defaults to `ingest.manifest.json`.
- `--tag`, `--note`, `--reading-status`, and `--priority` set **shared** manifest top-level fields only; they are not baked into each paper. The batch importer merges them with per-paper values.
- `--mailto` is sent in the Crossref `User-Agent` for the polite pool.
- `--delay` (default 3.0) is applied between **all** network requests. arXiv asks for at least 3 seconds between calls.

## Identifier formats

Each line of `--identifiers` is classified. Lines starting with `#` are comments; multiple identifiers may be comma-separated on one line.

| Input form | Resolved as |
| --- | --- |
| `10.1000/paper-1`, `doi:10.1000/paper-1`, `https://doi.org/10.1000/paper-1` | DOI via Crossref |
| `arXiv:2401.00001`, `https://arxiv.org/abs/2401.00001v2`, `arxiv.org/pdf/2401.00001` | arXiv via the arXiv API |
| `2401.00001` (new-style, with optional `vN`) | arXiv (bare new-style id) |
| Other `http(s)://` URLs | `unresolved` (no metadata can be auto-derived; supply an item directly) |
| Bare digits or unrecognized text | `unresolved` (identifiers are never guessed) |

## Metadata sources and mapping

- **DOI (Crossref)**: `GET https://api.crossref.org/works/{doi}`. Maps `type`, `title` (+`subtitle`), `author`, `container-title`, `published-print`/`published-online`/`issued`, `DOI`, `URL`, `volume`, `issue`, `page`, `publisher`, `language`, `abstract` (JATS/XML tags stripped), `ISSN`, `ISBN`. Crossref `type` maps to Zotero `itemType`: `journal-article`→`journalArticle`, `proceedings-article`→`conferencePaper`, `book-chapter`→`bookSection`, `book`→`book`, `report`→`report`, `posted-content`/`preprint`→`preprint`, `dissertation`→`thesis`, otherwise `journalArticle`. No PDF is derived for DOIs.
- **arXiv**: `GET https://arxiv.org/api/query?id_list={id}` (Atom XML). Maps `title`, `summary`→`abstractNote`, `author`→creators (single-name form), `published`→`date` (YYYY-MM), `arxiv:doi`→`DOI`, and `arxiv:comment`→the manifest `arxiv_comment` field (preserved verbatim; the importer adds the `Comment:` prefix). `itemType` is `preprint`, `archiveID` is `arXiv:{id}`, and `pdf_url` is `https://arxiv.org/pdf/{id}`.
- Page ranges are normalized: en dash (U+2013), em dash (U+2014), and TeX `--` are all collapsed to a single hyphen.

`--arxiv-base` derives the abs, PDF, and API URLs (default `https://arxiv.org`); it is hidden and intended for testing.

## BibTeX

`--bibtex` parses a `.bib` file locally (no network). Entry-type mapping: `article`→`journalArticle`, `inproceedings`/`conference`→`conferencePaper`, `book`→`book`, `phdthesis`/`mastersthesis`→`thesis`, `techreport`→`report`, `misc`/`unpublished`→`preprint`, otherwise `document`. Field mapping mirrors the Crossref mapping (`journal`/`journaltitle`→`publicationTitle`, `booktitle`→`proceedingsTitle`, `number`→`issue`, `address`→`place`, `doi`→`DOI`, `url`, `abstract`→`abstractNote`, `keywords`→entry-level `tags` split on `;`/`,`, `note`→entry-level `notes`, `language`). Authors are split on ` and `; `Last, First` and `First Last` are both accepted. `eprint` with `archivePrefix=arXiv` sets `archiveID`, `pdf_url`, and `referrer`. The BibTeX citekey becomes the manifest entry `id` (the stable ledger key).

`@string`, `@comment`, and `@preamble` entries are skipped. `@string` macros are **not** expanded; a value that is a bare macro name is kept as-is. The parser is best-effort: malformed entries are reported as `unresolved` with a reason and do not abort the run.

## Output

The manifest uses the schema documented in [batch-manifest.md](batch-manifest.md). Each paper entry carries its own derived `item`, `source_url`, optional `pdf_url`/`referrer`, and optional `arxiv_comment`. Shared `tags`/`notes`/`reading_status`/`priority` live at the manifest top level.

The stdout summary is structured JSON:

```json
{
  "status": "complete|completed_with_issues|failed",
  "input": "ids.txt",
  "collection": "Reading Queue",
  "manifest": "ids.manifest.json",
  "report": "ids.ingest-report.json",
  "total": 20,
  "resolved": 18,
  "unresolved": 2,
  "skipped_duplicate": 0,
  "with_pdf": 12,
  "needs_pdf": 6,
  "unresolved_items": [{"input": "...", "reason": "..."}]
}
```

- `with_pdf`: entries with a derivable `pdf_url` (arXiv, or BibTeX `eprint` with `archivePrefix=arXiv`).
- `needs_pdf`: resolved entries with no PDF (DOIs and most BibTeX entries). They require a PDF before a real import.
- When `resolved` is 0, `status` is `failed`, `manifest` is `null`, no manifest file is written, the report is still written, and the exit code is nonzero.
- Otherwise the exit code is 0 even when some identifiers are `unresolved`.

The optional `--report` file records every resolved entry and every `unresolved` reason for auditing.

## Working with `needs_pdf`

The batch importer requires a PDF for each entry during a real import. An entry lacking `pdf_file`, `pdf_url`, or `pdf_sources` is recorded as `invalid_manifest_entry`; other valid entries continue. For the cleanest unattended run:

1. Review the manifest, then run `zotero_batch_import.py --dry-run` to check metadata and possible duplicates without writing.
2. For every `needs_pdf` entry, add a PDF: download one through the agent's browser integration (legitimate access only) and set `pdf_file`, point `pdf_url` at a public direct PDF, or supply ordered `pdf_sources` fallbacks.
3. Alternatively, import as-is and inspect the per-entry failures, or trim to a subset where every entry has a PDF.

`--dry-run` checks metadata and duplicates but does not solve missing PDFs.

## Examples

### A list of identifiers

```text
Resolve ids.txt (DOIs and arXiv ids) into a manifest for the "Reading Queue" collection,
default each paper to to-read, and write an ingest report. Do not write to Zotero yet.
```

```text
python "<skill-dir>/scripts/zotero_ingest.py" \
  --identifiers ids.txt \
  --collection "Reading Queue" \
  --report ids.ingest-report.json
```

Inspect `ids.manifest.json`, then:

```text
python "<skill-dir>/scripts/zotero_batch_import.py" \
  --manifest ids.manifest.json \
  --ledger ids.zotero-results.jsonl
```

### A BibTeX file

```text
python "<skill-dir>/scripts/zotero_ingest.py" \
  --bibtex refs.bib \
  --collection "Thesis Sources" \
  --tag thesis \
  --priority medium
```

## Privacy and access

The resolver talks only to public Crossref and arXiv APIs over HTTPS. It does not bypass paywalls, CAPTCHAs, or access controls, and it does not persist cookies or session storage. PDF retrieval for `needs_pdf` entries still follows the same access rules as the rest of the skill.
