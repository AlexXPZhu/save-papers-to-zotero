---
name: save-papers-to-zotero
description: Import one paper or a batch into an exact Zotero collection through the local Connector server, with complete metadata, arXiv Comments child notes, Ethereal Style workflow tags, verified stored PDFs, possible-duplicate reporting, and resumable ledgers; use only when the user explicitly asks to save, import, repair, or verify papers in Zotero.
---

# Save Papers to Zotero

Complete explicit Zotero import requests through the desktop app's local Connector server on `127.0.0.1:23119`. Do not ask the user to invoke the Zotero Connector browser extension. Treat a parent item without a verified stored PDF as incomplete unless the user requests a dry run.

## Resolve Bundled Paths

Resolve the directory containing this `SKILL.md` before running an importer. In Claude Code, use `${CLAUDE_SKILL_DIR}`. In Codex, use the installed skill directory supplied with the loaded skill. Replace `<skill-dir>` below with that absolute directory, quote the resulting script path, and never assume that the current working directory is the skill directory. Use an available Python 3.10+ executable; the examples use `python`.

## Choose the Importer

- Use `<skill-dir>/scripts/zotero_connector_import.py` for one paper.
- Use `<skill-dir>/scripts/zotero_batch_import.py` for two or more papers, a DOI/article list, or literature-review output.
- Read `<skill-dir>/references/batch-manifest.md` before preparing a batch manifest.

Both importers:

1. verify Zotero connectivity and the exact target collection;
2. identify possible existing matches by DOI, arXiv ID, then normalized title;
3. report those matches but still create the requested new item;
4. save translator-style metadata and optional child notes, including arXiv `Comments`;
5. assign the requested collection, ordinary tags, default `#status/to-read`, and optional priority tags;
6. upload PDF bytes as a stored child attachment; and
7. verify that the newly created parent—not a pre-existing match—is in the collection with a stored PDF.

Never automatically delete, merge, replace, or suppress an item because a possible match exists. Return `possible_duplicate_count` and `possible_duplicate_keys` so the user can inspect Zotero and decide what to remove.

## Prepare Reliable Inputs

Resolve each paper to a canonical landing page. Prefer publisher pages, DOI records, PubMed Central, repositories, or preprint pages that the user may legitimately access. Populate all reliable metadata exposed by the source, including abstract, publication, volume, issue, pages, language, DOI, arXiv identifier, and full creator list. Do not invent missing values.

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

Obtain each PDF in one of two ways:

- Pass a public direct PDF URL and its landing page as the referrer.
- For a PDF available only in the user's signed-in Chrome session, use the host's browser integration: use the Chrome-control skill in Codex, or Claude in Chrome from a Claude Code session launched with `claude --chrome` (or with Chrome enabled by default). Download the visible legitimate PDF to a temporary file, then pass that file with `--pdf-file`. Never inspect, export, or persist cookies or session storage.

If the browser integration is unavailable, permission is denied, or the download does not produce a local valid PDF, stop before the Zotero write and ask the user to provide a legitimately obtained local PDF. Do not fall back to an untrusted mirror.

Do not bypass paywalls, CAPTCHAs, access controls, or licensing restrictions.

## Import One Paper

For a public PDF URL:

```text
python -X utf8 "<skill-dir>/scripts/zotero_connector_import.py" \
  --item-json <item.json> \
  --collection <exact collection name> \
  --source-url <landing page URL> \
  --pdf-url <direct PDF URL> \
  --referrer <landing page URL>
```

For a downloaded PDF, replace `--pdf-url` with `--pdf-file <path>`. Use `--pdf-source-url <URL>` with either `--pdf-file` or `--pdf-url` when the attachment's recorded source should differ from the download URL.

Use `--arxiv-comment <text>` for the source's arXiv `Comments` value; the importer adds `Comment: ` exactly once. Repeat `--note` for other child notes and `--tag` for ordinary tags. Reading status defaults to `to-read`; use `--reading-status reading` to override or `--reading-status none` to opt out. Use `--priority high|medium|low` only when explicitly requested or mapped from user-provided tiers. Add `--target-id <id>` only when multiple writable Connector targets have the exact requested name. Use `--dry-run` for connectivity, collection, input, and possible-duplicate checks without writing.

## Import a Batch

Prepare and validate every manifest entry before starting writes. Then run:

```text
python -X utf8 "<skill-dir>/scripts/zotero_batch_import.py" \
  --manifest <papers.json> \
  --collection <exact collection name> \
  --target-id <id> \
  --ledger <results.jsonl> \
  --summary-json <summary.json>
```

Omit the example's `--target-id` line unless same-named Connector targets are ambiguous. Every manifest entry defaults to `#status/to-read`; use shared or per-paper `reading_status` only to select `reading` or `none`. Set shared and per-paper `tags`, `notes`, and `priority` in the manifest; set each paper's `arxiv_comment` from its own arXiv source. The batch CLI intentionally has no `--tag` or `--note` options. Add `--stop-on-error` when no later paper should be attempted after the first incomplete entry.

The batch importer must remain serial. It revalidates the exact target immediately before every write, appends and flushes one ledger record after every paper, continues after paper-specific failures by default, and stops on Zotero connectivity or collection failures. Resume is enabled by default: rerunning the same manifest and ledger skips prior `saved_with_pdf` records. Use `--no-resume` only when the user explicitly wants prior completed requests written again.

Never parallelize writes or run two processes against the same ledger. The importer uses an operating-system advisory lock. Its `.lock` file may remain on disk after a run or crash; the file alone does not block a future batch.

Batch exit codes are `0` when all requests completed or were safely skipped, `3` when the run finished with incomplete entries, and another nonzero code for a fatal manifest, lock, connection, or collection error. The single importer returns `0` on success and a nonzero code on failure.

## Interpret Results

Treat importer JSON as authoritative:

- `saved_with_pdf`: new parent and stored PDF verified.
- `skipped_completed`: prior successful ledger record retained during resume.
- `skipped_duplicate_in_manifest`: repeated manifest entry skipped without a second write.
- `invalid_pdf`, `http_error`, or another paper-specific status: continue the batch and report the source and reason.
- `verification_failed`: metadata may have been saved, but the newly created item or its stored PDF could not be verified; do not rerun blindly.
- `not_attempted`: a fatal batch-level failure stopped later entries.

`possible_duplicate_count` and `possible_duplicate_keys` are informational. They refer only to items that existed before this request; the importer still writes and verifies a new parent. Present them for manual review and never remove any item without an explicit user decision.

When supplied, `arxiv_comment` reports the normalized child-note text. `workflow_tags` reports the applied default or explicit `#status/...` tag and any requested `#priority/...` tag. Include these in the completion report so the user can verify the workflow metadata without opening every item.

## Report the Batch

Report:

- total requested;
- newly saved with verified PDFs;
- resume-skipped and manifest-duplicate-skipped;
- newly saved items that had possible pre-existing matches, with their keys;
- needs user action;
- failed or not attempted; and
- the title, source, status, and reason for every incomplete paper.

Do not claim success because an attachment row or URL is visible. Require the importer's stored-file verification.
