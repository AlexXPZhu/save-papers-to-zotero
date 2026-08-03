<div align="center">

<img src="save-papers-to-zotero/assets/icon.png" alt="Save Papers to Zotero icon" width="180">

# Save Papers to Zotero

**A safety-first Codex and Claude Code skill for importing research papers into the exact Zotero collection you choose.**

[![Tests](https://github.com/AlexXPZhu/save-papers-to-zotero/actions/workflows/tests.yml/badge.svg)](https://github.com/AlexXPZhu/save-papers-to-zotero/actions/workflows/tests.yml)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Codex Skill](https://img.shields.io/badge/Codex-Skill-111827)](save-papers-to-zotero/)
[![Claude Code Skill](https://img.shields.io/badge/Claude%20Code-Skill-D97757)](save-papers-to-zotero/)
[![Zotero](https://img.shields.io/badge/Zotero-Local%20Connector-CC2936?logo=zotero&logoColor=white)](https://www.zotero.org/)

[English](README.md) · [简体中文](README.zh-CN.md)

[Why it helps](#why-this-skill) · [Quick start](#quick-start) · [Examples](#example-workflows) · [Safety](#safety-and-behavior) · [Troubleshooting](#troubleshooting)

</div>

> [!NOTE]
> This is an independent community project. It is not affiliated with or endorsed by Zotero, OpenAI, or Anthropic.

## Why this skill

This skill grew out of a practical research-workflow gap. ChatGPT, Codex, and Claude Code can help survey a topic and identify useful papers. They can also control Chrome to open those papers, but invoking the Zotero Connector browser extension does not provide a dependable, verifiable import into the right collection.

This skill bridges that gap through Zotero's local Connector server. It does more than download a PDF: it creates a structured Zotero item with dependable metadata, preserves information such as arXiv `Comments` in child notes, adds research-workflow tags, stores the PDF, and verifies the final item and attachment.

| Without a structured workflow | With this skill |
| --- | --- |
| Items can land in the wrong collection | The exact target is resolved and revalidated before every write |
| A successful request may still leave no usable PDF | The stored attachment is independently downloaded and verified |
| Comments, notes, and reading state are easy to lose | Child notes and Ethereal Style-compatible tags are applied consistently |
| Batch work can be difficult to resume safely | A manifest, ledger, and lock make serial imports resumable |
| Duplicate handling may be destructive or opaque | Possible duplicates are reported; nothing is deleted, merged, or replaced |

```mermaid
flowchart LR
    A[Canonical metadata and PDF] --> B[Preflight exact target]
    B --> C[Scan possible duplicates]
    C --> D[Create parent item]
    D --> E[Add notes and workflow tags]
    E --> F[Store PDF attachment]
    F --> G[Verify collection and stored file]
    G --> H[Return structured JSON result]
```

## Highlights

- Imports one paper or a batch into an exact Zotero collection.
- Resolves a list of DOIs/arXiv IDs or a `.bib` file into a reviewable manifest via Crossref and the arXiv API.
- Preserves complete metadata and arXiv Comments as child notes.
- Integrates with the Ethereal Style Zotero plugin by producing its `#status/...` and `#priority/...` workflow tags, without guessing priority.
- Verifies both collection membership and the locally stored PDF.
- Reports possible duplicates while honoring an explicit request to create a new item.
- Uses resumable batch ledgers and per-manifest locking.
- Uses Python's standard library only; no package installation is required.

## Quick start

### 1. Check the requirements

| Requirement | Details |
| --- | --- |
| Agent | Codex with skills, or Claude Code 2.1.211 or newer |
| Claude browser access | Optional: Claude in Chrome extension 1.0.36 or newer and a direct Anthropic plan |
| Python | Python 3.10 or newer; no third-party packages required |
| Zotero | Zotero desktop must be running |
| Local API | In Zotero settings, enable **Allow other applications on this computer to communicate with Zotero** |
| Ethereal Style | Recommended companion Zotero plugin for using the generated `#status/...` and `#priority/...` tags as a research workflow |
| PDF access | You must already have legitimate access to the PDF |

### 2. Install for Codex or Claude Code

#### Codex

The easiest method is to ask the built-in `$skill-installer` to install the skill directly from GitHub:

```text
Use $skill-installer to install:
https://github.com/AlexXPZhu/save-papers-to-zotero/tree/main/save-papers-to-zotero
```

The skill becomes available on your next Codex turn.

<details>
<summary>Manual installation fallback</summary>

Copy the repository's `save-papers-to-zotero` directory into your Codex skills directory:

```text
$CODEX_HOME/skills/save-papers-to-zotero
```

If `CODEX_HOME` is not set, the default location is `~/.codex/skills/save-papers-to-zotero`. Restart Codex or begin a new turn after copying it.

</details>

#### Claude Code

Add this repository as a marketplace, then install the plugin:

```text
/plugin marketplace add AlexXPZhu/save-papers-to-zotero
/plugin install save-papers-to-zotero@save-papers-to-zotero
```

Claude Code can invoke the skill automatically, or you can invoke it explicitly with `/save-papers-to-zotero:save-papers-to-zotero`. To receive repository updates, run:

```text
/plugin marketplace update save-papers-to-zotero
/plugin update save-papers-to-zotero@save-papers-to-zotero
```

To uninstall it, run `/plugin uninstall save-papers-to-zotero@save-papers-to-zotero`.

### 3. Prepare Zotero

1. Start Zotero desktop.
2. Create or identify the destination collection.
3. Use its exact name in your request. If names are ambiguous, supply the collection key with `--target-id`.

### 4. Ask Codex or Claude Code to import a paper

```text
Save https://arxiv.org/abs/1706.03762 to my Zotero collection "Reading Queue".
Tag it as to-read and add the arXiv Comments as a child note.
```

## Example workflows

### One paper

```text
Import DOI 10.1145/3290605.3300233 into the exact Zotero collection "HCI".
Save and verify the PDF, and use the reading workflow tag.
```

### A resumable batch

```text
Import every paper in manifest.json into "Thesis Sources" serially.
Keep a resumable ledger and report possible duplicates without deleting anything.
```

See the [batch manifest reference](save-papers-to-zotero/references/batch-manifest.md) for the schema and resume rules.

### From a list of identifiers

```text
Resolve ids.txt (DOIs and arXiv ids) into a manifest for "Reading Queue",
default each paper to to-read, and write an ingest report. Do not write to Zotero yet.
```

The resolver fetches metadata from Crossref and the arXiv API, preserves arXiv Comments, and writes a manifest you can review. arXiv entries get a derivable PDF; DOI entries are reported as `needs_pdf`. See the [ingest reference](save-papers-to-zotero/references/ingest.md).

### From a BibTeX file

```text
Resolve refs.bib into a manifest for "Thesis Sources" and tag every paper thesis.
```

The resolver parses the `.bib` locally, maps entry types and fields to Zotero items, and turns `eprint` + `archivePrefix=arXiv` into an arXiv `pdf_url`.

### Dry run first

```text
Dry-run this paper import into "Reading Queue" and show me the resolved collection,
metadata, tags, PDF source, and possible duplicates without writing to Zotero.
```

### A PDF requiring an authenticated browser session

```text
Save this publisher paper to "Reading Queue". I already have legitimate access in Chrome;
use the browser session only to obtain the PDF, and do not export cookies or session storage.
```

The skill does not bypass paywalls, CAPTCHAs, or access controls.

In Claude Code, launch the CLI with `claude --chrome` or enable Chrome by default with `/chrome`. The browser integration downloads the authorized PDF to a temporary local file; the importer then receives it through `--pdf-file`. If Chrome is unavailable or the download fails, the workflow stops before writing and asks you for a legitimate local PDF.

## Workflow tags

This skill is designed to work with the [Ethereal Style](https://github.com/MuiseDestiny/zotero-style) Zotero plugin. The skill writes ordinary Zotero tags in the naming convention Ethereal Style recognizes; the plugin can then use those tags in its reading-status and priority workflow. Zotero can store the tags without the plugin, but install Ethereal Style to get the intended integrated workflow.

| Requested state | Zotero tag |
| --- | --- |
| `to-read` (default) | `#status/to-read` |
| `reading` | `#status/reading` |
| `none` | No status tag |

Priority is optional and must be explicit: `high`, `medium`, or `low` maps to `#priority/high`, `#priority/medium`, or `#priority/low`. The skill never infers priority from a paper's content.

## Safety and behavior

| Guarantee | Behavior |
| --- | --- |
| Exact destination | The collection is resolved during preflight and revalidated immediately before writing |
| Non-destructive duplicates | Possible duplicates are reported; existing items are never deleted, merged, replaced, or used to silently suppress the requested item |
| Verified PDF | Success with a PDF requires a stored attachment whose bytes download successfully and validate as a PDF |
| Honest failure state | If post-write verification fails, the result says so and warns that the item may already exist |
| Resumable batches | Imports run serially with a ledger and lock; completed entries can be skipped safely |
| Access boundaries | The workflow does not defeat paywalls, CAPTCHAs, authentication, or other access controls |
| Session privacy | Browser-assisted retrieval must not export cookies or session storage |

## Result statuses

| Status | Meaning |
| --- | --- |
| `saved_with_pdf` | Parent item and stored PDF were created and verified |
| `ready` | Dry-run preflight completed; no Zotero write occurred |
| `skipped_completed` | A ledger entry was already completed |
| `skipped_duplicate_in_manifest` | A repeated entry was skipped within the same batch |
| `verification_failed` | A write occurred, but final verification did not pass; inspect Zotero before retrying |
| `not_attempted` | Import was not attempted because a required precondition failed |
| `invalid_pdf` / `http_error` | PDF retrieval or validation failed |

Every script emits structured JSON so a caller can distinguish success, skips, preflight failures, and uncertain post-write states.

## Troubleshooting

| Symptom or code | What to check |
| --- | --- |
| Cannot reach Zotero | Start Zotero desktop and confirm the local Connector server is available |
| `403` or `local_api_disabled` | Enable **Allow other applications on this computer to communicate with Zotero** in Zotero settings |
| `target_not_found` | Check the collection name and use the exact spelling |
| `target_ambiguous` | Supply the intended collection key with `--target-id` |
| `invalid_pdf` | Make sure the source is a direct, valid PDF available to your current access context |
| `verification_failed` | The item may already have been created; inspect Zotero instead of blindly retrying |
| Claude browser is unavailable | Launch with `claude --chrome`, then check `/chrome`; authenticated browser retrieval requires a direct Anthropic plan |
| Browser download failed | Download the legitimately accessible PDF yourself and provide its local path; the skill does not switch to an untrusted mirror |

## Compatibility and tests

The test suite uses a fake Zotero server and covers metadata, attachments, verification, duplicates, resumable batch behavior, and dual-platform packaging. GitHub Actions runs it on Python 3.10 and 3.12 across Windows, Linux, and macOS, and validates the Claude marketplace with Claude Code 2.1.211.

The current release was also exercised against Zotero 9.0.6 and Connector API v3 on July 18, 2026.

```powershell
python -X utf8 -m unittest discover -s tests -v
```

## Repository layout

```text
save-papers-to-zotero/
├── .claude-plugin/marketplace.json
├── .github/workflows/tests.yml
├── save-papers-to-zotero/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── references/batch-manifest.md
│   ├── references/ingest.md
│   └── scripts/
├── tests/
│   ├── test_importers.py
│   ├── test_ingest.py
│   └── test_skill_packaging.py
├── README.md
├── README.zh-CN.md
└── LICENSE
```

## Development and privacy

Contributions and focused bug reports are welcome. Keep real PDFs, manifests, ledgers, and other private research data outside the repository or under the ignored `local-data/` directory. The import workflow communicates with Zotero over its loopback Connector server and should never persist browser cookies or session storage.

To test the Claude plugin directly from a checkout, run `claude --plugin-dir ./save-papers-to-zotero --chrome`, then use `/reload-plugins` after changing plugin files. Run `claude plugin validate .` from the repository root before publishing.

## License

Released under the [MIT License](LICENSE).
