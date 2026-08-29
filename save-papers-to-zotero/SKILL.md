---
name: save-papers-to-zotero
description: Import one paper or a batch into an exact Zotero collection through the local Connector server, with complete metadata, optional Chinese full-text summary notes, arXiv Comments, Ethereal Style workflow tags, verified stored PDFs, possible-duplicate reporting, and resumable ledgers; use only when the user explicitly asks to save, import, repair, summarize while importing, or verify papers in Zotero.
---

# Save Papers to Zotero

Complete explicit Zotero import requests through the desktop app's local Connector server on `127.0.0.1:23119`. Do not ask the user to invoke the Zotero Connector browser extension. Treat a parent item without a verified stored PDF as incomplete unless the user requests a dry run. The target collection must already exist in Zotero before you run an importer; the local Connector server cannot create collections through its API, so create one manually in the Zotero app first if it is missing.

## Resolve Bundled Paths

Resolve the directory containing this `SKILL.md` before running an importer. In Claude Code, use `${CLAUDE_SKILL_DIR}`. In Codex, use the installed skill directory supplied with the loaded skill. Replace `<skill-dir>` below with that absolute directory, quote the resulting script path, and never assume that the current working directory is the skill directory. Use Python 3.10 or newer. Resolve the launcher once: prefer `py -3` on Windows when available, otherwise `python3`, then `python`; substitute that command for `<python>` below.

## Choose the Importer

- Use `<skill-dir>/scripts/zotero_connector_import.py` for one paper.
- Use `<skill-dir>/scripts/zotero_batch_import.py` for two or more papers, a prepared manifest, or literature-review output.
- Use `<skill-dir>/scripts/zotero_ingest.py` when you only have a list of DOIs/arXiv IDs or a `.bib` file. It resolves metadata and writes a manifest you review before importing; see [Ingest from Identifier Lists or BibTeX](#ingest-from-identifier-lists-or-bibtex) and `<skill-dir>/references/ingest.md`.
- Read `<skill-dir>/references/batch-manifest.md` before preparing a batch manifest.
- Read `<skill-dir>/references/summarization.md` only when the user explicitly requests AI summaries or Chinese reading guides during import.

Both importers:

1. verify Zotero connectivity and the exact target collection, which must already exist in Zotero (the local server cannot create collections through its API; create it manually in the Zotero app if needed);
2. identify possible existing matches by DOI, arXiv ID, then normalized title;
3. report those matches but still create the requested new item;
4. save translator-style metadata and optional child notes, including arXiv `Comments`;
5. assign the requested collection, ordinary tags, default `#status/to-read`, and optional priority tags;
6. stream PDF bytes as a stored child attachment; and
7. verify that the newly created parent—not a pre-existing match—is in the collection and that the stored PDF's size and SHA-256 match the source.

Never automatically delete, merge, replace, or suppress an item because a possible match exists. Return `possible_duplicate_count` and `possible_duplicate_keys` so the user can inspect Zotero and decide what to remove.

## Prepare Reliable Inputs

Resolve each paper to a canonical landing page. Prefer publisher pages, DOI records, PubMed Central, repositories, or preprint pages that the user may legitimately access. Populate all reliable metadata exposed by the source, including abstract, publication, volume, issue, pages, language, DOI, arXiv identifier, and full creator list. Do not invent missing values. To obtain reliable metadata from a list of DOIs/arXiv IDs or a `.bib` file, use the ingest resolver in [Ingest from Identifier Lists or BibTeX](#ingest-from-identifier-lists-or-bibtex) instead of assembling items by hand.

For an arXiv paper, copy the source's `Comments` field verbatim except for whitespace normalization. Pass only its value; the importer creates a separate child note beginning `Comment: `. Preserve venue, code, project, and publication information contained there. Do not synthesize an arXiv comment from the abstract, citation, or your own analysis, and omit it when the source has no `Comments` field.

Use ordinary Zotero tags in the `#status/...` and `#priority/...` forms recognized by the Ethereal Style plugin; no special Zotero field or tag type is required. Default every newly imported paper to `#status/to-read` unless another status or an explicit opt-out is supplied:

- `reading_status: to-read` becomes `#status/to-read`;
- `reading_status: reading` becomes `#status/reading`;
- `reading_status: none` disables automatic status assignment; and
- `priority: high`, `medium`, or `low` becomes `#priority/high`, `#priority/medium`, or `#priority/low`.

Preserve an explicit `#status/...` tag already present in the item or requested tags instead of replacing it with the default. A supplied semantic status replaces conflicting status tags. Never infer priority from paper content; add it only from a user value or an explicit mapping such as a user-defined tier.

Use a Zotero translator-style item object:

```json
{
  "itemType": "preprint",
  "title": "Example Paper",
  "creators": [
    {"firstName": "Ada", "lastName": "Lovelace", "creatorType": "author"}
  ],
  "date": "2026",
  "url": "https://example.org/paper",
  "DOI": "10.0000/example",
  "archiveID": "arXiv:2601.00001"
}
```

Obtain each PDF in one of these ways:

- Pass a public direct PDF URL and its landing page as the referrer.
- For a PDF available only in the user's signed-in Chrome session, use the host's browser integration: use the Chrome-control skill in Codex, or Claude in Chrome from a Claude Code session launched with `claude --chrome` (or with Chrome enabled by default). Download the visible legitimate PDF to a temporary file, then pass that file with `--pdf-file`. Never inspect, export, or persist cookies or session storage.
- For a batch with more than one legitimate candidate, put them in ordered `pdf_sources`. The importer exhausts the configured retries for one source before trying the next, and records every attempted source.

If the browser integration is unavailable, permission is denied, or the download does not produce a local valid PDF, stop before the Zotero write and ask the user to provide a legitimately obtained local PDF. Do not fall back to an untrusted mirror.

Do not bypass paywalls, CAPTCHAs, access controls, or licensing restrictions.

## Generate Optional Chinese Summaries

Do not generate a summary unless the user explicitly requests it. Summary generation uses the current agent's context and can consume roughly 30,000-60,000 tokens per full paper. Recommend 1-5 papers per turn and never summarize more than 10; ask the user to split larger requests before any Zotero write.

Follow `<skill-dir>/references/summarization.md` exactly. Before reading any PDF, run the intended importer with `--dry-run`. For batches, do not summarize `skipped_completed` or `skipped_duplicate_in_manifest` entries. Obtain a local PDF and analyze its full text with the host's PDF capability; if PDF rendering or extraction is unavailable, use a confirmed full-text HTML edition of the same paper as specified in the reference. Render the model's strict JSON through `<skill-dir>/scripts/zotero_summary_note.py`, attach the resulting safe HTML through the existing `notes` field, and pass the same local PDF to the real importer with `--pdf-file`.

Treat summarization as best-effort enrichment. `pdf_unavailable`, `needs_ocr`, `extraction_failed`, `summary_failed`, and `note_verification_failed` do not block an otherwise valid PDF import. Never use the abstract as a silent full-text fallback, install OCR for this MVP, use PDF page numbers for focus locations, or insert model-authored HTML directly. After a successful summary-bearing import, run the helper's separate `verify` command against the new `item_key`; never add note queries to the importer's default verification path.

## Import One Paper

For a public PDF URL:

```text
<python> "<skill-dir>/scripts/zotero_connector_import.py" \
  --item-json <item.json> \
  --collection <exact collection name> \
  --source-url <landing page URL> \
  --pdf-url <direct PDF URL> \
  --referrer <landing page URL>
```

For a downloaded PDF, replace `--pdf-url` with `--pdf-file <path>`. Use `--pdf-source-url <URL>` with either `--pdf-file` or `--pdf-url` when the attachment's recorded source should differ from the download URL.

Use `--arxiv-comment <text>` for the source's arXiv `Comments` value; the importer adds `Comment: ` exactly once. Repeat `--note` for other child notes and `--tag` for ordinary tags. Reading status defaults to `to-read`; use `--reading-status reading` to override or `--reading-status none` to opt out. Use `--priority high|medium|low` only when explicitly requested or mapped from user-provided tiers. Add `--target-id <id>` only when multiple writable Connector targets have the exact requested name. `--dry-run` is an optional diagnostic, not a required precondition for a normal import. The separate high-cost summarization workflow is the exception: it requires a dry run before consuming model tokens.

## Import a Batch

Prepare a manifest, then run:

```text
<python> "<skill-dir>/scripts/zotero_batch_import.py" \
  --manifest <papers.json> \
  --collection <exact collection name> \
  --target-id <id> \
  --ledger <results.jsonl> \
  --summary-json <summary.json> \
  --safety-level balanced
```

Omit the example's `--target-id` line unless same-named Connector targets are ambiguous. Every manifest entry defaults to `#status/to-read`; use shared or per-paper `reading_status` only to select `reading` or `none`. Set shared and per-paper `tags`, `notes`, and `priority` in the manifest; set each paper's `arxiv_comment` from its own arXiv source. The batch CLI intentionally has no `--tag` or `--note` options. Add `--stop-on-error` when no later paper should be attempted after the first incomplete entry.

The batch importer must remain serial. It appends and flushes one ledger record after every paper, continues after paper-specific and per-entry manifest failures by default, and stops after exhausted Zotero connectivity retries or a collection failure. PDF downloads default to three attempts with exponential backoff and jitter; source failures remain paper-specific and may fall back to the next `pdf_sources` entry. Progress events go to stderr while stdout remains one final JSON document.

Safety defaults to `balanced`: resolve the full collection once per batch, lightly confirm the target once before the first write, and verify every result. Use `fast` only when the user accepts one initial preflight with no extra prewrite confirmation. Use `strict` when the collection may change during the run; it repeats the full target and collection checks for every paper. Do not run a dry run merely to compensate for normal uncertainty—the default balanced path is designed for direct execution.

Resume is enabled by default. It is scoped to the same base URL, collection, and requested target ID; unscoped legacy records and records from another collection are not trusted for skipping. Repeated resumes retain the original `saved_with_pdf` evidence. If a prior write saved metadata but verification did not finish, resume returns `skipped_needs_review` instead of creating a possible duplicate. Use `--no-resume` only when the user explicitly accepts another write.

Never parallelize writes or run two processes against the same ledger. The importer uses an operating-system advisory lock. Its `.lock` file may remain on disk after a run or crash; the file alone does not block a future batch.

Batch exit codes are `0` when all requests completed or were safely skipped, `3` when the run finished with incomplete entries, and another nonzero code for a fatal manifest, lock, connection, or collection error. The single importer returns `0` on success and a nonzero code on failure.

## Ingest from Identifier Lists or BibTeX

When you have only a list of DOIs / arXiv IDs or a `.bib` file, resolve them into a manifest first with `<skill-dir>/scripts/zotero_ingest.py`. It fetches metadata from Crossref (DOIs) and the arXiv API (arXiv ids), or parses BibTeX locally, and writes a manifest that `zotero_batch_import.py` consumes. It does not contact Zotero. Read `<skill-dir>/references/ingest.md` for the supported formats, field mappings, and `needs_pdf` handling before using it.

```text
<python> "<skill-dir>/scripts/zotero_ingest.py" \
  --identifiers <ids.txt|-> \
  --collection "<exact collection name>" \
  [--out <manifest.json>] [--report <ingest-report.json>] \
  [--tag <t> ...] [--note <n> ...] \
  [--reading-status to-read|reading|none] [--priority high|medium|low]
```

Use `--bibtex <refs.bib>` instead of `--identifiers` for a BibTeX file. `--tag`, `--note`, `--reading-status`, and `--priority` set shared manifest top-level fields; they are not baked into each paper. The resolver reports every identifier it could not resolve and continues; it never drops failures silently. arXiv entries get a derivable `pdf_url`; DOI entries do not, so the summary reports `needs_pdf`.

A real import requires a PDF for each entry. In a mixed manifest, entries without a PDF are recorded as `invalid_manifest_entry` while valid entries continue. Prefer adding legitimate `pdf_file`, `pdf_url`, or ordered `pdf_sources` values before import; trimming the manifest is optional. A dry run can check metadata and duplicates but does not supply missing PDFs.

## Interpret Results

Treat importer JSON as authoritative:

- `saved_with_pdf`: new parent and stored PDF verified.
- `skipped_completed`: prior successful ledger record retained during resume.
- `skipped_needs_review`: a prior write may have saved metadata, so automatic retry was suppressed to avoid a duplicate.
- `skipped_duplicate_in_manifest`: repeated manifest entry skipped without a second write.
- `invalid_manifest_entry`: this entry is malformed; other valid entries continue unless `--stop-on-error` is set.
- `invalid_pdf`, `pdf_http_error`, `pdf_sources_exhausted`, or another paper-specific status: continue the batch and report the source and reason.
- `verification_failed`: metadata may have been saved, but the newly created item or its stored PDF could not be verified; do not rerun blindly.
- `not_attempted`: a fatal batch-level failure stopped later entries.

`possible_duplicate_count` and `possible_duplicate_keys` are informational. They refer only to items that existed before this request; the importer still writes and verifies a new parent. Present them for manual review and never remove any item without an explicit user decision.

When supplied, `arxiv_comment` reports the normalized child-note text. `workflow_tags` reports the applied default or explicit `#status/...` tag and any requested `#priority/...` tag. Include these in the completion report so the user can verify the workflow metadata without opening every item.

For an explicitly requested summary, separately report `note_verified`, `pdf_unavailable`, `needs_ocr`, `extraction_failed`, `summary_failed`, or `note_verification_failed`. A summary-specific failure never changes a verified `saved_with_pdf` result. Resume-skipped items are not modified and receive no new summary in this MVP.

## Report the Batch

Use `task_results` and `counts` for the effective whole-task result across resumes; use `results` and `this_run_counts` only to explain the latest invocation. Report:

- total requested;
- newly saved with verified PDFs;
- resume-skipped and manifest-duplicate-skipped;
- newly saved items that had possible pre-existing matches, with their keys;
- needs user action;
- failed or not attempted; and
- the title, source, status, and reason for every incomplete paper.

Do not claim success because an attachment row or URL is visible. Require the importer's stored-file verification.
