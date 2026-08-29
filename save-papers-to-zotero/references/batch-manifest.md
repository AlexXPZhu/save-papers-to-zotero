# Batch Manifest

Use this schema with `scripts/zotero_batch_import.py`. The importer accepts either the object form below or a bare array of paper entries. Prefer the object form so the collection and shared tags are explicit.

A manifest conforming to this schema can also be produced by `scripts/zotero_ingest.py` from a list of DOI/arXiv identifiers or a `.bib` file; see [ingest.md](ingest.md). Manifests produced that way carry shared `tags`/`notes`/`reading_status`/`priority` at the top level and only paper-specific derived fields on each entry.

## Manifest fields

- `collection`: exact Zotero collection name. Must already exist in Zotero; the local server cannot create collections through its API, so a missing collection fails the import with `target_not_found`. Optional only when `--collection` is passed.
- `target_id`: optional Connector target ID. Use it only when multiple writable Connector targets share the exact requested collection name; `--target-id` overrides it.
- `tags`: optional tags added to every paper.
- `notes`: optional child-note strings added to every paper.
- `reading_status`: optional shared Ethereal Style status, `to-read`, `reading`, or `none`; omitted entries default to `to-read`.
- `priority`: optional shared Ethereal Style priority, `high`, `medium`, or `low`.
- `papers`: required non-empty array.

Each paper entry supports:

- `id`: optional stable ledger key. Otherwise DOI, arXiv ID, or normalized title is used.
- `item`: inline Zotero translator-style item object.
- `item_json`: path to one translator-style item JSON file, relative to the manifest.
- `source_url`: canonical landing page.
- exactly one legacy `pdf_url` or `pdf_file`, or one non-empty ordered `pdf_sources` array, for a real import;
- `pdf_source_url`: attachment source URL recorded for either a local `pdf_file` or downloaded `pdf_url`;
- `pdf_title`: attachment title, default `PDF`;
- `referrer`: landing page used when downloading `pdf_url`;
- `tags`: additional tags for this paper;
- `notes`: additional child-note strings for this paper;
- `arxiv_comment`: the paper's arXiv `Comments` value, saved as a separate `Comment: ...` child note;
- `reading_status`: optional per-paper override, including `none` to disable automatic status assignment; and
- `priority`: optional per-paper override of the shared priority.

Do not specify both `item` and `item_json`, or both `pdf_url` and `pdf_file`. A dry run may omit both PDF fields.

Each object in `pdf_sources` requires exactly one `pdf_url` or `pdf_file` and may override `pdf_source_url`, `pdf_title`, and `referrer`. Sources are tried in array order before any Zotero metadata write. A source uses its normal retry budget before the next source is attempted, while the per-paper wall timeout covers the entire source list. Unknown paper and source keys are rejected as `invalid_manifest_entry` so misspellings are not ignored.

Copy `arxiv_comment` only from the source's arXiv `Comments` field. Do not include the `Comment:` prefix unless it is already present; the importer normalizes it to exactly one prefix. Do not place AI-generated summaries or reading notes in this field—use `notes` for those.

When the user explicitly requests a Chinese full-text guide, follow [summarization.md](summarization.md) to render safe note HTML before adding it to the per-paper `notes` array.

Workflow fields create ordinary Zotero tags in the `#status/...` and `#priority/...` forms recognized by the Ethereal Style plugin; they do not require a special Zotero field or tag type. An omitted status defaults to `#status/to-read`; `reading` maps to `#status/reading`; `none` opts out. An explicit `#status/...` already present in the item or requested tags takes precedence over the default. `high`, `medium`, and `low` map to `#priority/...`, but priority has no default and must never be inferred from paper content. Per-paper values override shared values.

The importer allows matches already present in the Zotero library: it creates a new parent and reports the pre-existing keys in `possible_duplicate_keys` for manual review. It still skips a repeated request inside the same manifest and, by default, skips requests already completed in the same ledger. It never deletes, merges, replaces, or suppresses library items automatically.

## Example

```json
{
  "collection": "Route A",
  "tags": ["video-llm", "research-batch"],
  "priority": "medium",
  "papers": [
    {
      "item": {
        "itemType": "preprint",
        "title": "Example Paper One",
        "creators": [
          {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}
        ],
        "date": "2026",
        "url": "https://arxiv.org/abs/2601.00001",
        "archiveID": "arXiv:2601.00001"
      },
      "source_url": "https://arxiv.org/abs/2601.00001",
      "pdf_sources": [
        {
          "pdf_url": "https://publisher.example/paper-one.pdf",
          "referrer": "https://publisher.example/paper-one"
        },
        {
          "pdf_url": "https://arxiv.org/pdf/2601.00001",
          "referrer": "https://arxiv.org/abs/2601.00001",
          "pdf_title": "arXiv PDF"
        }
      ],
      "arxiv_comment": "Accepted at ExampleConf 2026. Code: https://github.com/example/project",
      "notes": ["<p>Imported from the literature review.</p>"]
    },
    {
      "item_json": "items/paper-two.json",
      "source_url": "https://publisher.example/paper-two",
      "pdf_file": "pdfs/paper-two.pdf",
      "pdf_source_url": "https://publisher.example/paper-two.pdf"
    }
  ]
}
```

## Output files

The ledger is append-only JSONL. Every line contains the run ID, UTC timestamp, manifest index, stable request key, title, source, status, resume scope, and status-specific details. Keep it for retries and auditing. Resume trusts prior successes only when base URL, normalized collection, and requested target ID match. Legacy unscoped records are retained for audit but do not cause an automatic skip.

Each successful write includes `possible_duplicate_count` and `possible_duplicate_keys`. These fields report matches that existed before the new item was created; they do not mean the new write was skipped. When supplied, `arxiv_comment` contains the normalized `Comment: ...` child-note text and `workflow_tags` lists the applied Ethereal Style tags.

The optional summary JSON is written atomically. `results` and `this_run_counts` describe the current invocation. `task_results` and `counts` reconstruct the effective whole task, retaining original success evidence through repeated resumes. `historical_failure_items` reports earlier failures that later succeeded, while `currently_unresolved` lists outstanding entries. A batch exits with code `0` only when every entry is complete or safely skipped; code `3` means the batch finished with incomplete or needs-review entries; other nonzero codes indicate a manifest, lock, connection, or collection failure. Use `--stop-on-error` to mark all later entries `not_attempted` after the first incomplete entry.
