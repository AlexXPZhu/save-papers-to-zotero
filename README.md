# Save Papers to Zotero

`save-papers-to-zotero` is a Codex skill for importing one paper or a batch into an exact Zotero collection through Zotero's local Connector server. It saves complete metadata, arXiv Comments notes, workflow tags, and stored PDFs, then verifies the newly created Zotero item and attachment.

## Features

- Imports one paper or a resumable serial batch.
- Resolves the exact writable Zotero collection before every write.
- Reports possible DOI, arXiv ID, and normalized-title matches without deleting or merging anything.
- Preserves arXiv `Comments` as child notes.
- Supports Ethereal Style `#status/...` and `#priority/...` tags.
- Uploads PDFs as stored attachments and verifies that the files exist.
- Records batch outcomes in an append-only JSONL ledger.

## Requirements

- A Codex installation that supports skills.
- Python 3.10 or newer. The importers use only the Python standard library.
- Zotero desktop running on the same computer.
- Zotero's **Allow other applications on this computer to communicate with Zotero** setting enabled.
- Legitimate access to each PDF being imported.

The integration uses Zotero's local API for verification and its Connector HTTP server for writes. See the official [Zotero Local API](https://www.zotero.org/support/dev/web_api/v3/local_api) and [Connector HTTP Server](https://www.zotero.org/support/dev/client_coding/connector_http_server) documentation.

## Install

Copy the [`save-papers-to-zotero`](save-papers-to-zotero/) directory into the personal skills directory configured for your Codex installation. A common location is `$CODEX_HOME/skills`; when `CODEX_HOME` is unset, use `~/.codex/skills`.

The resulting path should end with:

```text
skills/save-papers-to-zotero/SKILL.md
```

Start a new Codex task after installation so the skill metadata is discovered.

## Use

Invoke the skill explicitly when you want to import papers:

```text
Use $save-papers-to-zotero to import these papers into my exact Zotero collection and verify every stored PDF.
```

The skill defaults new items to `#status/to-read`. It never infers priority and never automatically deletes, merges, replaces, or suppresses possible duplicates.

For batch manifest fields and examples, see [`batch-manifest.md`](save-papers-to-zotero/references/batch-manifest.md).

## Development

Run the test suite from the repository root:

```text
python -X utf8 tests/test_importers.py -v
```

The tests use a local fake Zotero server and do not modify a real Zotero library. GitHub Actions runs them on Python 3.10 and 3.12 across Linux, Windows, and macOS.

Keep real manifests, PDFs, ledgers, and summaries outside the repository or under an ignored `local-data/` directory.

## License

Released under the [MIT License](LICENSE).
