#!/usr/bin/env python3
"""Resolve DOI/arXiv identifiers or BibTeX entries into a batch import manifest.

This script produces a manifest that ``zotero_batch_import.py`` consumes. It
does not contact Zotero and does not write to any library. It resolves metadata
from public APIs (Crossref, arXiv) or parses a local BibTeX file, reports
per-identifier failures without dropping them silently, and emits a reviewable
manifest plus an optional ingest report.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path

import zotero_connector_import as single


DEFAULT_CROSSREF_BASE = "https://api.crossref.org"
DEFAULT_ARXIV_BASE = "https://arxiv.org"
DEFAULT_MAILTO = "save-papers-to-zotero@users.noreply.github.com"
DEFAULT_DELAY = 3.0
DEFAULT_HTTP_TIMEOUT = 30.0

DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
ARXIV_BARE_PATTERN = re.compile(r"\d{4}\.\d{4,5}(?:v\d+)?$", re.IGNORECASE)

CROSSREF_TYPE_MAP = {
    "journal-article": "journalArticle",
    "proceedings-article": "conferencePaper",
    "book-chapter": "bookSection",
    "book": "book",
    "report": "report",
    "posted-content": "preprint",
    "preprint": "preprint",
    "dissertation": "thesis",
}

BIBTEX_TYPE_MAP = {
    "article": "journalArticle",
    "inproceedings": "conferencePaper",
    "conference": "conferencePaper",
    "book": "book",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "techreport": "report",
    "misc": "preprint",
    "unpublished": "preprint",
    "booklet": "document",
    "manual": "document",
    "incollection": "bookSection",
}

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}

# Page-range normalization: en dash (U+2013) and em dash (U+2014) -> hyphen,
# then collapse any run of hyphens to one.
_PAGE_DASHES = str.maketrans({"–": "-", "—": "-"})


class IngestFailure(Exception):
    def __init__(self, status: str, message: str, exit_code: int = 2, **details):
        super().__init__(message)
        self.status = status
        self.message = message
        self.exit_code = exit_code
        self.details = details


def normalize_pages(value: str) -> str:
    value = value.translate(_PAGE_DASHES)
    value = re.sub(r"-{2,}", "-", value)
    return " ".join(value.split())


def _first(value: object) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0]).strip() if value[0] else None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _strip_tags(text: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", "", text).split())


# ---------------------------------------------------------------- identifiers


def classify_identifier(raw: str) -> tuple[str, str] | None:
    text = raw.strip()
    if not text:
        return None
    match = single.ARXIV_PATTERN.search(text)
    if match:
        return ("arxiv", match.group(1).casefold())
    match = DOI_PATTERN.search(text)
    if match:
        doi = single.canonical_doi(match.group(0))
        if doi:
            return ("doi", doi)
    if ARXIV_BARE_PATTERN.fullmatch(text):
        aid = re.sub(r"(?i)v\d+$", "", text).casefold()
        return ("arxiv", aid)
    if text.lower().startswith(("http://", "https://")):
        return ("url", text)
    return None


def _http_get(url: str, headers: dict[str, str], timeout: float) -> bytes:
    _, payload, _ = single.request_bytes(url, headers=headers, timeout=timeout)
    return payload


# ------------------------------------------------------------------- crossref


def crossref_to_item(message: dict, doi: str) -> dict:
    item: dict = {
        "itemType": CROSSREF_TYPE_MAP.get(
            str(message.get("type", "")).casefold(), "journalArticle"
        )
    }
    title = _first(message.get("title"))
    subtitle = _first(message.get("subtitle"))
    if title and subtitle:
        title = f"{title}: {subtitle}"
    if title:
        item["title"] = title
    authors: list[dict] = []
    for author in message.get("author", []) or []:
        if not isinstance(author, dict):
            continue
        given = (author.get("given") or "").strip()
        family = (author.get("family") or "").strip()
        if family or given:
            authors.append(
                {"creatorType": "author", "firstName": given, "lastName": family}
            )
    if authors:
        item["creators"] = authors
    container = _first(message.get("container-title"))
    if container:
        item["publicationTitle"] = container
    issued = (
        message.get("published-print")
        or message.get("published-online")
        or message.get("issued")
    )
    date_parts = (issued or {}).get("date-parts") if isinstance(issued, dict) else None
    if isinstance(date_parts, list) and date_parts and isinstance(date_parts[0], list):
        formatted = []
        for index, part in enumerate(date_parts[0]):
            try:
                formatted.append(f"{int(part):02d}" if index > 0 else str(int(part)))
            except (TypeError, ValueError):
                formatted.append(str(part))
        if formatted:
            item["date"] = "-".join(formatted)
    item["DOI"] = message.get("DOI") or doi
    if message.get("URL"):
        item["url"] = str(message["URL"])
    if message.get("volume"):
        item["volume"] = str(message["volume"])
    if message.get("issue"):
        item["issue"] = str(message["issue"])
    page = message.get("page")
    if page:
        item["pages"] = normalize_pages(str(page))
    if message.get("publisher"):
        item["publisher"] = str(message["publisher"])
    if message.get("language"):
        item["language"] = str(message["language"])
    abstract = message.get("abstract")
    if abstract:
        item["abstractNote"] = _strip_tags(str(abstract))
    issn = _first(message.get("ISSN"))
    if issn:
        item["ISSN"] = issn
    isbn = _first(message.get("ISBN"))
    if isbn:
        item["ISBN"] = isbn
    return item


def fetch_crossref(
    doi: str, *, mailto: str, timeout: float, base_url: str
) -> dict:
    url = f"{base_url.rstrip('/')}/works/{urllib.parse.quote(doi, safe='/')}"
    payload = _http_get(
        url,
        {"Accept": "application/json", "User-Agent": f"save-papers-to-zotero/1.0 (mailto:{mailto})"},
        timeout,
    )
    data = json.loads(payload.decode("utf-8"))
    message = data.get("message", {}) if isinstance(data, dict) else {}
    if not isinstance(message, dict) or not message:
        raise IngestFailure("unresolved", f"Crossref returned no record for {doi}")
    item = crossref_to_item(message, doi)
    return {
        "item": item,
        "source_url": f"https://doi.org/{doi}",
        "pdf_url": None,
        "arxiv_comment": None,
    }


# --------------------------------------------------------------------- arxiv


def _atom_text(entry: object, path: str) -> str | None:
    element = entry.find(path, ATOM_NS)  # type: ignore[union-attr]
    if element is None or element.text is None:
        return None
    return " ".join(element.text.split())


def arxiv_entry_to_item(entry: object, arxiv_id: str, base_url: str) -> dict:
    item: dict = {"itemType": "preprint"}
    title = _atom_text(entry, "atom:title")
    if title:
        item["title"] = title
    authors: list[dict] = []
    for author in entry.findall("atom:author", ATOM_NS):  # type: ignore[union-attr]
        name = _atom_text(author, "atom:name")
        if name:
            authors.append({"creatorType": "author", "name": name})
    if authors:
        item["creators"] = authors
    summary = _atom_text(entry, "atom:summary")
    if summary:
        item["abstractNote"] = summary
    published = _atom_text(entry, "atom:published")
    if published:
        item["date"] = published[:7]
    doi = _atom_text(entry, "arxiv:doi")
    if doi:
        item["DOI"] = doi
    item["archiveID"] = f"arXiv:{arxiv_id}"
    abs_url = f"{base_url.rstrip('/')}/abs/{arxiv_id}"
    item["url"] = abs_url
    comment = _atom_text(entry, "arxiv:comment")
    return {
        "item": item,
        "source_url": abs_url,
        "pdf_url": f"{base_url.rstrip('/')}/pdf/{arxiv_id}",
        "referrer": abs_url,
        "arxiv_comment": comment,
    }


def fetch_arxiv(arxiv_id: str, *, timeout: float, base_url: str) -> dict:
    url = f"{base_url.rstrip('/')}/api/query?id_list={urllib.parse.quote(arxiv_id)}"
    payload = _http_get(
        url,
        {"Accept": "application/atom+xml", "User-Agent": "save-papers-to-zotero/1.0"},
        timeout,
    )
    root = ET.fromstring(payload.decode("utf-8"))
    entry = root.find("atom:entry", ATOM_NS)
    if entry is None:
        raise IngestFailure("unresolved", f"arXiv returned no entry for {arxiv_id}")
    return arxiv_entry_to_item(entry, arxiv_id, base_url)


# -------------------------------------------------------------------- bibtex


def _scan_entry(text: str, pos: int) -> tuple[str | None, int]:
    """``pos`` is the opening brace/paren. Return (inner_text, end_index)."""
    n = len(text)
    opener = text[pos]
    depth = 1
    i = pos + 1
    chars: list[str] = []
    while i < n:
        char = text[i]
        if char == "{":
            depth += 1
            chars.append(char)
        elif char == "}":
            depth -= 1
            if depth == 0 and opener == "{":
                return "".join(chars), i + 1
            chars.append(char)
        elif char == ")" and opener == "(" and depth == 1:
            return "".join(chars), i + 1
        else:
            chars.append(char)
        i += 1
    return None, n


def _split_top_commas(text: str, max_splits: int | None = None) -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    count = 0
    for char in text:
        if char == "{":
            depth += 1
            current.append(char)
        elif char == "}":
            depth -= 1
            current.append(char)
        elif char == "," and depth == 0 and (max_splits is None or count < max_splits):
            parts.append("".join(current))
            current = []
            count += 1
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _split_top_hash(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    in_string = False
    current: list[str] = []
    for char in text:
        if char == "{":
            depth += 1
            current.append(char)
        elif char == "}":
            depth -= 1
            current.append(char)
        elif char == '"' and depth == 0:
            in_string = not in_string
            current.append(char)
        elif char == "#" and depth == 0 and not in_string:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _parse_single_value(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    if value[0] == "{" and value.endswith("}"):
        return value[1:-1]
    if value[0] == '"' and value.endswith('"') and len(value) >= 2:
        return value[1:-1]
    return value


def _parse_value(value: str) -> str:
    return "".join(_parse_single_value(piece) for piece in _split_top_hash(value))


def _parse_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for chunk in _split_top_commas(text):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip().lower()
        if not name:
            continue
        parsed = _parse_value(value.strip())
        if parsed != "":
            fields[name] = parsed
    return fields


def _parse_entry_body(body: str) -> tuple[str, dict[str, str]]:
    parts = _split_top_commas(body, max_splits=1)
    citekey = parts[0].strip()
    fields_text = parts[1] if len(parts) > 1 else ""
    return citekey, _parse_fields(fields_text)


def parse_bibtex(text: str) -> list[tuple[str, str | None, dict[str, str], str | None]]:
    """Yield (entry_type, citekey, fields, error). error is None on success."""
    results: list[tuple[str, str | None, dict[str, str], str | None]] = []
    n = len(text)
    i = 0
    while i < n:
        at = text.find("@", i)
        if at < 0:
            break
        j = at + 1
        while j < n and (text[j].isalnum() or text[j] in "_-:"):
            j += 1
        entry_type = text[at + 1 : j].lower()
        if not entry_type:
            i = at + 1
            continue
        while j < n and text[j].isspace():
            j += 1
        if j >= n or text[j] not in "{(":
            i = at + 1
            continue
        body, end = _scan_entry(text, j)
        if body is None:
            results.append((entry_type, None, {}, f"@{entry_type}: unterminated entry"))
            i = j + 1
            continue
        try:
            citekey, fields = _parse_entry_body(body)
        except Exception as error:  # noqa: BLE001 - best-effort parser
            results.append((entry_type, None, {}, f"@{entry_type}: {error}"))
            i = end
            continue
        results.append((entry_type, citekey, fields, None))
        i = end
    return results


def _parse_bibtex_authors(value: str) -> list[dict]:
    authors: list[dict] = []
    for raw in re.split(r"\s+and\s+", value):
        raw = raw.strip()
        if not raw:
            continue
        if "," in raw:
            last, _, first = raw.partition(",")
            authors.append(
                {"creatorType": "author", "firstName": first.strip(), "lastName": last.strip()}
            )
        else:
            parts = raw.split()
            if len(parts) == 1:
                authors.append({"creatorType": "author", "name": parts[0]})
            else:
                authors.append(
                    {
                        "creatorType": "author",
                        "firstName": " ".join(parts[:-1]),
                        "lastName": parts[-1],
                    }
                )
    return authors


def _split_keywords(value: str) -> list[str]:
    return [token.strip() for token in re.split(r"[;,]", value) if token.strip()]


def bibtex_entry_to_manifest_entry(
    citekey: str, entry_type: str, fields: dict[str, str], arxiv_base: str
) -> dict:
    item: dict = {"itemType": BIBTEX_TYPE_MAP.get(entry_type, "document")}
    if "title" in fields:
        item["title"] = fields["title"]
    if "author" in fields:
        item["creators"] = _parse_bibtex_authors(fields["author"])
    if "date" in fields:
        item["date"] = fields["date"]
    elif "year" in fields:
        item["date"] = fields["year"]
    publication = fields.get("journal") or fields.get("journaltitle")
    if publication:
        item["publicationTitle"] = publication
    if "booktitle" in fields:
        item["proceedingsTitle"] = fields["booktitle"]
    if "volume" in fields:
        item["volume"] = fields["volume"]
    if "number" in fields:
        item["issue"] = fields["number"]
    if "pages" in fields:
        item["pages"] = normalize_pages(fields["pages"])
    if "publisher" in fields:
        item["publisher"] = fields["publisher"]
    if "address" in fields:
        item["place"] = fields["address"]
    if "doi" in fields:
        item["DOI"] = single.canonical_doi(fields["doi"]) or fields["doi"]
    if "url" in fields:
        item["url"] = fields["url"]
    if "abstract" in fields:
        item["abstractNote"] = fields["abstract"]
    if "language" in fields:
        item["language"] = fields["language"]

    entry: dict = {"id": citekey, "item": item}
    if "doi" in fields:
        entry["source_url"] = f"https://doi.org/{item.get('DOI', fields['doi'])}"
    elif "url" in fields:
        entry["source_url"] = fields["url"]

    eprint = (fields.get("eprint") or "").strip()
    archive_prefix = (fields.get("archiveprefix") or "").lower()
    if eprint and "arxiv" in archive_prefix:
        item["archiveID"] = f"arXiv:{eprint}"
        entry["pdf_url"] = f"{arxiv_base.rstrip('/')}/pdf/{eprint}"
        entry["referrer"] = f"{arxiv_base.rstrip('/')}/abs/{eprint}"
        if not entry.get("source_url"):
            entry["source_url"] = f"{arxiv_base.rstrip('/')}/abs/{eprint}"

    if "keywords" in fields:
        tags = _split_keywords(fields["keywords"])
        if tags:
            entry["tags"] = tags
    if "note" in fields:
        entry["notes"] = [fields["note"]]
    return entry


# -------------------------------------------------------------- manifest build


def build_manifest(
    collection: str,
    target_id: str | None,
    entries: list[dict],
    *,
    tags: list[str],
    notes: list[str],
    reading_status: str | None,
    priority: str | None,
) -> dict:
    manifest: dict = {"collection": collection}
    if target_id:
        manifest["target_id"] = target_id
    if tags:
        manifest["tags"] = tags
    if notes:
        manifest["notes"] = notes
    if reading_status:
        manifest["reading_status"] = reading_status
    if priority:
        manifest["priority"] = priority
    manifest["papers"] = entries
    return manifest


def _entry_from_resolved(resolved: dict, request_key: str) -> dict:
    item = single.validate_item(resolved["item"])
    entry: dict = {"id": request_key, "item": item}
    if resolved.get("source_url"):
        entry["source_url"] = resolved["source_url"]
    if resolved.get("pdf_url"):
        entry["pdf_url"] = resolved["pdf_url"]
    if resolved.get("referrer"):
        entry["referrer"] = resolved["referrer"]
    comment = resolved.get("arxiv_comment")
    if comment:
        entry["arxiv_comment"] = comment
    return entry


def resolve_identifiers(
    identifiers: list[str],
    *,
    mailto: str,
    delay: float,
    timeout: float,
    crossref_base: str,
    arxiv_base: str,
) -> tuple[list[dict], list[dict], list[dict]]:
    entries: list[dict] = []
    unresolved: list[dict] = []
    duplicates: list[dict] = []
    seen: set[str] = set()
    for raw in identifiers:
        text = raw.strip()
        if not text or text.startswith("#"):
            continue
        classified = classify_identifier(text)
        if classified is None:
            unresolved.append(
                {"input": text, "reason": "Could not classify as a DOI or arXiv identifier"}
            )
            continue
        kind, value = classified
        if kind == "url":
            unresolved.append(
                {"input": text, "reason": "URL did not contain a DOI or arXiv id; supply metadata directly"}
            )
            continue
        request_key = f"{kind}:{value}"
        if request_key in seen:
            duplicates.append({"input": text, "id": request_key})
            continue
        seen.add(request_key)
        if delay:
            time.sleep(delay)
        resolved = None
        for attempt in range(2):
            try:
                if kind == "doi":
                    resolved = fetch_crossref(
                        value, mailto=mailto, timeout=timeout, base_url=crossref_base
                    )
                else:
                    resolved = fetch_arxiv(value, timeout=timeout, base_url=arxiv_base)
                break
            except single.ImportFailure as error:
                # arXiv intermittently resets TLS connections; give one
                # transient connection error a second chance before reporting.
                if error.status == "connection_error" and attempt == 0:
                    if delay:
                        time.sleep(delay)
                    continue
                unresolved.append({"input": text, "reason": error.message})
                break
            except IngestFailure as error:
                unresolved.append({"input": text, "reason": error.message})
                break
            except Exception as error:  # noqa: BLE001 - record and continue
                unresolved.append({"input": text, "reason": f"{type(error).__name__}: {error}"})
                break
        if resolved is None:
            continue
        try:
            entry = _entry_from_resolved(resolved, request_key)
        except single.ImportFailure as error:
            unresolved.append({"input": text, "reason": f"Invalid metadata: {error.message}"})
            continue
        except Exception as error:  # noqa: BLE001 - record and continue
            unresolved.append({"input": text, "reason": f"{type(error).__name__}: {error}"})
            continue
        entries.append(entry)
    return entries, unresolved, duplicates


def resolve_bibtex(
    text: str, *, arxiv_base: str
) -> tuple[list[dict], list[dict], list[dict]]:
    entries: list[dict] = []
    unresolved: list[dict] = []
    duplicates: list[dict] = []
    seen: set[str] = set()
    for entry_type, citekey, fields, error in parse_bibtex(text):
        if error:
            unresolved.append({"input": entry_type, "reason": error})
            continue
        if entry_type in ("comment", "string", "preamble"):
            continue
        if not citekey:
            unresolved.append({"input": f"@{entry_type}", "reason": "BibTeX entry has no citekey"})
            continue
        if citekey in seen:
            duplicates.append({"input": citekey, "id": citekey})
            continue
        seen.add(citekey)
        try:
            entry = bibtex_entry_to_manifest_entry(citekey, entry_type, fields, arxiv_base)
            single.validate_item(entry["item"])
        except single.ImportFailure as error:
            unresolved.append({"input": citekey, "reason": f"Invalid metadata: {error.message}"})
            continue
        except Exception as error:  # noqa: BLE001 - record and continue
            unresolved.append({"input": citekey, "reason": f"{type(error).__name__}: {error}"})
            continue
        entries.append(entry)
    return entries, unresolved, duplicates


def _read_identifiers(source: str) -> list[str]:
    if source == "-":
        text = sys.stdin.read()
    else:
        text = Path(source).read_text(encoding="utf-8")
    tokens: list[str] = []
    for line in text.splitlines():
        if line.strip().startswith("#"):
            continue
        for chunk in line.split(","):
            chunk = chunk.strip()
            if chunk:
                tokens.append(chunk)
    return tokens


def _default_out_path(identifiers: str | None, bibtex: Path | None) -> Path:
    if bibtex is not None:
        return bibtex.with_suffix(".manifest.json")
    if identifiers and identifiers != "-":
        return Path(identifiers).with_suffix(".manifest.json")
    return Path("ingest.manifest.json")


def run(args: argparse.Namespace) -> dict:
    collection = (args.collection or "").strip()
    if not collection:
        raise IngestFailure("invalid_args", "Collection name is required")
    reading_status = single.canonical_workflow_value(
        args.reading_status, "reading_status", single.READING_STATUS_OPTIONS
    )
    priority = single.canonical_workflow_value(args.priority, "priority", single.PRIORITIES)

    if args.bibtex is not None:
        try:
            text = args.bibtex.read_text(encoding="utf-8")
        except OSError as error:
            raise IngestFailure("invalid_bibtex", f"Cannot read BibTeX file: {error}") from error
        entries, unresolved, duplicates = resolve_bibtex(text, arxiv_base=args.arxiv_base)
        input_name = str(args.bibtex)
    else:
        identifiers = _read_identifiers(args.identifiers)
        entries, unresolved, duplicates = resolve_identifiers(
            identifiers,
            mailto=args.mailto,
            delay=args.delay,
            timeout=args.http_timeout,
            crossref_base=args.crossref_base,
            arxiv_base=args.arxiv_base,
        )
        input_name = args.identifiers

    with_pdf = sum(1 for entry in entries if entry.get("pdf_url") or entry.get("pdf_file"))
    needs_pdf = sum(1 for entry in entries if not (entry.get("pdf_url") or entry.get("pdf_file")))

    manifest_path: Path | None = None
    if entries:
        manifest = build_manifest(
            collection,
            args.target_id,
            entries,
            tags=args.tag,
            notes=args.note,
            reading_status=reading_status,
            priority=priority,
        )
        manifest_path = args.out or _default_out_path(args.identifiers, args.bibtex)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    report = {
        "input": input_name,
        "collection": collection,
        "resolved": len(entries),
        "unresolved": unresolved,
        "skipped_duplicate": len(duplicates),
        "entries": entries,
    }
    report_path: Path | None = None
    if args.report:
        report_path = args.report
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    total = len(entries) + len(unresolved) + len(duplicates)
    if not entries:
        status = "failed"
    elif unresolved or duplicates:
        status = "completed_with_issues"
    else:
        status = "complete"
    return {
        "status": status,
        "input": input_name,
        "collection": collection,
        "manifest": str(manifest_path) if manifest_path else None,
        "report": str(report_path) if report_path else None,
        "total": total,
        "resolved": len(entries),
        "unresolved": len(unresolved),
        "skipped_duplicate": len(duplicates),
        "with_pdf": with_pdf,
        "needs_pdf": needs_pdf,
        "unresolved_items": unresolved,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--identifiers",
        help="Path to a file of DOI/arXiv identifiers (one per line or comma-separated), or - for stdin",
    )
    source.add_argument("--bibtex", type=Path, help="Path to a BibTeX (.bib) file")
    parser.add_argument("--collection", required=True, help="Exact target collection name")
    parser.add_argument("--target-id", help="Connector target ID baked into the manifest")
    parser.add_argument("--out", type=Path, help="Manifest output path")
    parser.add_argument("--report", type=Path, help="Optional per-identifier ingest report path")
    parser.add_argument("--tag", action="append", default=[], help="Shared tag; repeat for multiple")
    parser.add_argument(
        "--note", action="append", default=[], help="Shared child note; repeat for multiple"
    )
    parser.add_argument(
        "--reading-status",
        choices=sorted(single.READING_STATUS_OPTIONS),
        help="Ethereal Style reading status baked into the manifest top level",
    )
    parser.add_argument(
        "--priority", choices=sorted(single.PRIORITIES), help="Ethereal Style priority"
    )
    parser.add_argument("--mailto", default=DEFAULT_MAILTO, help="Contact email for the Crossref polite pool")
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help="Seconds between network requests (arXiv requires >=3s)",
    )
    parser.add_argument("--http-timeout", type=float, default=DEFAULT_HTTP_TIMEOUT)
    parser.add_argument("--crossref-base", default=DEFAULT_CROSSREF_BASE, help=argparse.SUPPRESS)
    parser.add_argument("--arxiv-base", default=DEFAULT_ARXIV_BASE, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        summary = run(args)
    except IngestFailure as error:
        print(json.dumps({"status": error.status, "message": error.message, **error.details}, ensure_ascii=False, indent=2))
        return error.exit_code
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["resolved"] > 0 else 1


if __name__ == "__main__":
    single.configure_utf8_stdio()
    sys.exit(main())
