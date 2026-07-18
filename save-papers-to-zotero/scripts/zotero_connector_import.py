#!/usr/bin/env python3
"""Import one paper and its PDF through Zotero's local Connector server."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypedDict


DEFAULT_BASE_URL = "http://127.0.0.1:23119"
CONNECTOR_HEADERS = {
    "Content-Type": "application/json",
    "X-Zotero-Connector-API-Version": "3",
}
ARXIV_PATTERN = re.compile(
    r"(?i)(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/)([a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})(?:v\d+)?"
)
READING_STATUSES = {"to-read", "reading"}
READING_STATUS_OPTIONS = READING_STATUSES | {"none"}
PRIORITIES = {"high", "medium", "low"}
STATUS_TAG_PREFIX = "#status/"
PRIORITY_TAG_PREFIX = "#priority/"

ImportStatus = Literal["ready", "saved_with_pdf"]


class CandidateInspection(TypedDict):
    item_key: str
    title: str | None
    in_target_collection: bool
    pdf_count: int
    pdf_verified: bool


class ImportResult(TypedDict, total=False):
    status: ImportStatus
    title: str
    identity: str
    collection: str
    target_id: str
    zotero_version: str | None
    item_key: str
    in_target_collection: bool
    pdf_count: int
    pdf_verified: bool
    possible_duplicate_count: int
    possible_duplicate_keys: list[str]
    arxiv_comment: str
    workflow_tags: list[str]


@dataclass(frozen=True)
class ImportContext:
    base_url: str
    collection: str
    target_id: str | None
    target: dict
    collection_key: str
    zotero_version: str | None


class ImportFailure(Exception):
    def __init__(self, status: str, message: str, exit_code: int = 2, **details):
        super().__init__(message)
        self.status = status
        self.message = message
        self.exit_code = exit_code
        self.details = details


def normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def canonical_doi(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    doi = value.strip()
    doi = re.sub(r"(?i)^https?://(?:dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"(?i)^doi\s*:\s*", "", doi)
    return doi.strip().casefold() or None


def arxiv_id(item: dict) -> str | None:
    for field in ("archiveID", "extra", "url"):
        value = item.get(field)
        if not isinstance(value, str):
            continue
        match = ARXIV_PATTERN.search(value)
        if match:
            return match.group(1).casefold()
    return None


def item_identity(item: dict) -> str:
    doi = canonical_doi(item.get("DOI"))
    if doi:
        return "doi:" + doi
    arxiv = arxiv_id(item)
    if arxiv:
        return "arxiv:" + arxiv
    return "title:" + normalized(str(item.get("title", "")))


def request_bytes(
    url: str,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> tuple[int, bytes, object]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        payload = error.read()
        text = payload.decode("utf-8", errors="replace")
        raise ImportFailure(
            "http_error",
            f"{method} {urllib.parse.urlsplit(url).path} returned HTTP {error.code}: {text[:300]}",
        ) from error
    except urllib.error.URLError as error:
        raise ImportFailure("connection_error", f"Cannot reach {url}: {error.reason}") from error


def connector_post(base_url: str, path: str, payload: dict) -> tuple[int, bytes, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return request_bytes(
        base_url + path,
        method="POST",
        body=body,
        headers=CONNECTOR_HEADERS,
    )


def api_get(base_url: str, path: str) -> object:
    _, payload, _ = request_bytes(
        base_url + path,
        headers={"Zotero-API-Version": "3", "Accept": "application/json"},
    )
    return json.loads(payload.decode("utf-8"))


def data_object(wrapper: dict) -> dict:
    return wrapper.get("data", wrapper)


def wrapper_key(wrapper: dict) -> str | None:
    return wrapper.get("key") or data_object(wrapper).get("key")


def find_target(base_url: str, collection_name: str, explicit_target_id: str | None) -> dict:
    _, payload, _ = connector_post(base_url, "/connector/getSelectedCollection", {})
    response = json.loads(payload.decode("utf-8"))
    targets = response.get("targets", [])

    if explicit_target_id:
        matches = [target for target in targets if target.get("id") == explicit_target_id]
        if len(matches) != 1:
            raise ImportFailure("target_not_found", f"Zotero target {explicit_target_id!r} was not found")
        target = matches[0]
        if collection_name and normalized(target.get("name", "")) != normalized(collection_name):
            raise ImportFailure(
                "target_mismatch",
                f"Target {explicit_target_id!r} is named {target.get('name')!r}, not {collection_name!r}",
            )
    else:
        matches = [
            target
            for target in targets
            if normalized(target.get("name", "")) == normalized(collection_name)
        ]
        if not matches:
            raise ImportFailure("target_not_found", f"No editable Zotero target named {collection_name!r} was found")
        if len(matches) > 1:
            ids = [target.get("id") for target in matches]
            raise ImportFailure(
                "target_ambiguous",
                f"Multiple Zotero targets are named {collection_name!r}; pass --target-id",
                target_ids=ids,
            )
        target = matches[0]

    if not target.get("filesEditable", False):
        raise ImportFailure("files_not_editable", f"Target {collection_name!r} does not accept file attachments")
    return target


def find_collection_key(base_url: str, collection_name: str) -> str | None:
    collections: list[dict] = []
    start = 0
    while True:
        try:
            page = api_get(
                base_url,
                f"/api/users/0/collections?format=json&include=data&limit=100&start={start}",
            )
        except ImportFailure:
            return None
        if not isinstance(page, list):
            return None
        collections.extend(page)
        if len(page) < 100:
            break
        start += len(page)
    matches = [
        wrapper_key(wrapper)
        for wrapper in collections
        if normalized(data_object(wrapper).get("name", "")) == normalized(collection_name)
    ]
    matches = [key for key in matches if key]
    return matches[0] if len(matches) == 1 else None


def paginated_item_query(base_url: str, search_text: str) -> list[dict]:
    results: list[dict] = []
    start = 0
    while True:
        query = urllib.parse.urlencode(
            {
                "q": search_text,
                "itemType": "-attachment",
                "format": "json",
                "include": "data",
                "limit": 100,
                "start": start,
            }
        )
        try:
            page = api_get(base_url, "/api/users/0/items?" + query)
        except ImportFailure as error:
            if error.status == "http_error" and "HTTP 403" in error.message:
                raise ImportFailure(
                    "local_api_disabled",
                    "Enable Zotero Settings > Advanced > Allow other applications on this computer to communicate with Zotero",
                ) from error
            raise
        if not isinstance(page, list):
            raise ImportFailure("invalid_api_response", "Zotero local API returned a non-list item response")
        results.extend(page)
        if len(page) < 100:
            break
        start += len(page)
    return results


def item_matches(candidate: dict, wanted: dict) -> bool:
    data = data_object(candidate)
    wanted_doi = canonical_doi(wanted.get("DOI"))
    candidate_doi = canonical_doi(data.get("DOI"))
    if wanted_doi and candidate_doi and wanted_doi == candidate_doi:
        return True
    wanted_arxiv = arxiv_id(wanted)
    candidate_arxiv = arxiv_id(data)
    if wanted_arxiv and candidate_arxiv and wanted_arxiv == candidate_arxiv:
        return True
    return normalized(data.get("title", "")) == normalized(wanted.get("title", ""))


def matching_item_candidates(base_url: str, item: dict) -> list[dict]:
    queries: list[str] = []
    doi = canonical_doi(item.get("DOI"))
    arxiv = arxiv_id(item)
    if doi:
        queries.append(doi)
    if arxiv:
        queries.append(arxiv)
    queries.append(item["title"])

    matches: dict[str, dict] = {}
    for query in dict.fromkeys(queries):
        for wrapper in paginated_item_query(base_url, query):
            key = wrapper_key(wrapper)
            if key and item_matches(wrapper, item):
                matches[key] = wrapper
    return list(matches.values())


def candidate_keys(candidates: list[dict]) -> list[str]:
    return sorted(key for candidate in candidates if (key := wrapper_key(candidate)))


def attachment_file_exists(base_url: str, attachment_key: str) -> bool:
    try:
        _, payload, _ = request_bytes(
            f"{base_url}/api/users/0/items/{attachment_key}/file/view/url",
            headers={"Zotero-API-Version": "3"},
        )
    except ImportFailure:
        return False
    value = payload.decode("utf-8", errors="replace").strip()
    if not value.startswith("file:"):
        return False
    parsed = urllib.parse.urlparse(value)
    path = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    if os.name == "nt" and path.startswith(("/", "\\")) and len(path) > 2 and path[2] == ":":
        path = path[1:]
    return os.path.isfile(path)


def inspect_candidate(base_url: str, wrapper: dict, target_collection_key: str) -> CandidateInspection:
    data = data_object(wrapper)
    item_key = wrapper_key(wrapper)
    children = api_get(
        base_url,
        f"/api/users/0/items/{item_key}/children?itemType=attachment&format=json&include=data&limit=100",
    )
    pdfs = []
    for child in children:
        child_data = data_object(child)
        if child_data.get("contentType", "").casefold() != "application/pdf":
            continue
        key = wrapper_key(child)
        pdfs.append(
            {
                "key": key,
                "title": child_data.get("title"),
                "file_exists": bool(key and attachment_file_exists(base_url, key)),
            }
        )
    collections = data.get("collections", [])
    return {
        "item_key": item_key,
        "title": data.get("title"),
        "in_target_collection": target_collection_key in collections,
        "pdf_count": len(pdfs),
        "pdf_verified": any(pdf["file_exists"] for pdf in pdfs),
    }


def validate_item(item: object) -> dict:
    if not isinstance(item, dict):
        raise ImportFailure("invalid_item_json", "Item JSON must contain one object")
    item = copy.deepcopy(item)
    for field in ("itemType", "title"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ImportFailure("invalid_item_json", f"Item JSON requires a non-empty {field!r}")
    if "creators" in item and not isinstance(item["creators"], list):
        raise ImportFailure("invalid_item_json", "Item JSON field 'creators' must be an array")
    if "notes" in item and not isinstance(item["notes"], list):
        raise ImportFailure("invalid_item_json", "Item JSON field 'notes' must be an array")
    if "tags" in item and not isinstance(item["tags"], list):
        raise ImportFailure("invalid_item_json", "Item JSON field 'tags' must be an array")
    item.pop("key", None)
    item.pop("version", None)
    item["attachments"] = []
    return item


def canonical_workflow_value(value: str | None, field: str, allowed: set[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ImportFailure(f"invalid_{field}", f"{field.replace('_', ' ').title()} must be a non-empty string")
    canonical = value.strip().casefold()
    if canonical not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ImportFailure(f"invalid_{field}", f"{field.replace('_', ' ').title()} must be one of: {choices}")
    return canonical


def format_arxiv_comment(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ImportFailure("invalid_arxiv_comment", "arXiv comment must be a non-empty string")
    comment = " ".join(value.split())
    comment = re.sub(r"(?i)^comment\s*:\s*", "", comment).strip()
    if not comment:
        raise ImportFailure("invalid_arxiv_comment", "arXiv comment must contain text after 'Comment:'")
    return "Comment: " + comment


def append_notes_and_tags(
    item: dict,
    notes: list[str] | None,
    tags: list[str] | None,
    *,
    arxiv_comment: str | None = None,
    reading_status: str | None = None,
    priority: str | None = None,
) -> tuple[dict, list[str]]:
    item = validate_item(item)
    comment_note = format_arxiv_comment(arxiv_comment)
    requested_notes = list(notes or [])
    if comment_note:
        requested_notes.insert(0, comment_note)
    if requested_notes:
        existing_notes = item.setdefault("notes", [])
        existing_note_values = {
            normalized(note.get("note", ""))
            for note in existing_notes
            if isinstance(note, dict)
        }
        for note in requested_notes:
            if not isinstance(note, str) or not note.strip():
                raise ImportFailure("invalid_note", "Notes must be non-empty strings")
            if normalized(note) not in existing_note_values:
                existing_notes.append({"note": note})
                existing_note_values.add(normalized(note))

    requested_tag_values: list[str] = []
    for tag in tags or []:
        if not isinstance(tag, str) or not tag.strip():
            raise ImportFailure("invalid_tag", "Tags must be non-empty strings")
        value = tag.strip()
        if value not in requested_tag_values:
            requested_tag_values.append(value)

    status_setting = canonical_workflow_value(
        reading_status,
        "reading_status",
        READING_STATUS_OPTIONS,
    )
    existing_item_tags = [
        tag.get("tag") if isinstance(tag, dict) else tag
        for tag in item.get("tags", [])
    ]
    has_explicit_status_tag = any(
        isinstance(tag, str) and tag.casefold().startswith(STATUS_TAG_PREFIX)
        for tag in existing_item_tags + requested_tag_values
    )
    if status_setting == "none" or (status_setting is None and has_explicit_status_tag):
        canonical_status = None
    else:
        canonical_status = status_setting or "to-read"
    canonical_priority = canonical_workflow_value(priority, "priority", PRIORITIES)
    semantic_tags = {
        STATUS_TAG_PREFIX: STATUS_TAG_PREFIX + canonical_status if canonical_status else None,
        PRIORITY_TAG_PREFIX: PRIORITY_TAG_PREFIX + canonical_priority if canonical_priority else None,
    }

    session_tags = requested_tag_values
    for prefix, semantic_tag in semantic_tags.items():
        if semantic_tag:
            session_tags = [tag for tag in session_tags if not tag.casefold().startswith(prefix)]
            session_tags.append(semantic_tag)
    if session_tags:
        existing_tags = item.setdefault("tags", [])
        for prefix, semantic_tag in semantic_tags.items():
            if semantic_tag:
                existing_tags[:] = [
                    tag
                    for tag in existing_tags
                    if not (
                        isinstance(tag, str) and tag.casefold().startswith(prefix)
                        or isinstance(tag, dict)
                        and isinstance(tag.get("tag"), str)
                        and tag["tag"].casefold().startswith(prefix)
                    )
                ]
        existing_values = {
            tag.get("tag") if isinstance(tag, dict) else tag
            for tag in existing_tags
        }
        for tag in session_tags:
            if tag not in existing_values:
                existing_tags.append({"tag": tag})
    return item, session_tags


def load_item(path: Path) -> dict:
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ImportFailure("invalid_item_json", f"Cannot read item JSON: {error}") from error
    return validate_item(item)


def read_pdf_source(
    *,
    pdf_file: Path | None,
    pdf_url: str | None,
    pdf_source_url: str | None,
    source_url: str,
    referrer: str | None,
    download_timeout: float,
) -> tuple[bytes, str]:
    if pdf_file:
        try:
            payload = Path(pdf_file).read_bytes()
        except OSError as error:
            raise ImportFailure("pdf_read_failed", f"Cannot read PDF file: {error}") from error
        recorded_source = pdf_source_url or source_url
    else:
        headers = {"User-Agent": "Mozilla/5.0 Zotero-Automation/1.0", "Accept": "application/pdf"}
        if referrer:
            headers["Referer"] = referrer
        _, payload, _ = request_bytes(str(pdf_url), headers=headers, timeout=download_timeout)
        recorded_source = pdf_source_url or str(pdf_url)
    if not payload.startswith(b"%PDF-"):
        raise ImportFailure("invalid_pdf", "The supplied attachment does not begin with a PDF signature")
    return payload, recorded_source


def upload_pdf(
    base_url: str,
    session_id: str,
    parent_item_id: str,
    pdf: bytes,
    source_url: str,
    title: str,
) -> None:
    metadata = json.dumps(
        {
            "sessionID": session_id,
            "title": title,
            "parentItemID": parent_item_id,
            "url": source_url,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    status, _, _ = request_bytes(
        base_url + "/connector/saveAttachment",
        method="POST",
        body=pdf,
        headers={
            "Content-Type": "application/pdf",
            "Content-Length": str(len(pdf)),
            "X-Zotero-Connector-API-Version": "3",
            "X-Metadata": metadata,
        },
        timeout=60,
    )
    if status != 201:
        raise ImportFailure("attachment_save_failed", f"Zotero returned HTTP {status} while saving the PDF")


def verify_new_item(
    base_url: str,
    item: dict,
    target_collection_key: str,
    timeout: float,
    preexisting_keys: set[str],
) -> CandidateInspection:
    deadline = time.monotonic() + timeout
    last: CandidateInspection | None = None
    while time.monotonic() < deadline:
        candidates = [
            candidate
            for candidate in matching_item_candidates(base_url, item)
            if wrapper_key(candidate) not in preexisting_keys
        ]
        if candidates:
            inspected = [inspect_candidate(base_url, candidate, target_collection_key) for candidate in candidates]
            eligible = [candidate for candidate in inspected if candidate["in_target_collection"]]
            eligible.sort(key=lambda candidate: candidate["pdf_verified"], reverse=True)
            if eligible:
                last = eligible[0]
                if last["pdf_verified"]:
                    return last
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.75, remaining))
    raise ImportFailure(
        "verification_failed",
        "The parent item or stored PDF could not be verified before the timeout",
        metadata_saved=True,
        last_observation=last,
    )


def prepare_context(base_url: str, collection: str, target_id: str | None = None) -> ImportContext:
    status, _, response_headers = connector_post(base_url, "/connector/ping", {})
    if status != 200:
        raise ImportFailure("zotero_unavailable", f"Zotero ping returned HTTP {status}")
    target = find_target(base_url, collection, target_id)
    collection_key = find_collection_key(base_url, target["name"])
    if collection_key is None:
        raise ImportFailure(
            "collection_verification_unavailable",
            f"Could not resolve a unique local API collection key for {target['name']!r}",
        )
    return ImportContext(
        base_url=base_url,
        collection=collection,
        target_id=target_id,
        target=target,
        collection_key=collection_key,
        zotero_version=response_headers.get("X-Zotero-Version"),
    )


def revalidate_context(context: ImportContext, base_url: str, collection: str) -> ImportContext:
    if context.base_url != base_url or normalized(context.collection) != normalized(collection):
        raise ImportFailure("context_mismatch", "Prepared Zotero context does not match this import request")
    fresh = prepare_context(base_url, collection, context.target_id)
    if fresh.target.get("id") != context.target.get("id") or fresh.collection_key != context.collection_key:
        raise ImportFailure(
            "context_changed",
            f"Zotero target {collection!r} changed during the import; stopping before another write",
        )
    return fresh


def import_item(
    *,
    item: dict,
    collection: str,
    target_id: str | None = None,
    pdf_file: Path | None = None,
    pdf_url: str | None = None,
    pdf_source_url: str | None = None,
    pdf_title: str = "PDF",
    source_url: str | None = None,
    referrer: str | None = None,
    notes: list[str] | None = None,
    tags: list[str] | None = None,
    arxiv_comment: str | None = None,
    reading_status: str | None = None,
    priority: str | None = None,
    download_timeout: float = 60,
    verify_timeout: float = 30,
    dry_run: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    context: ImportContext | None = None,
) -> ImportResult:
    item, session_tags = append_notes_and_tags(
        item,
        notes,
        tags,
        arxiv_comment=arxiv_comment,
        reading_status=reading_status,
        priority=priority,
    )
    canonical_comment = format_arxiv_comment(arxiv_comment)
    enrichment_details: dict[str, object] = {}
    if canonical_comment:
        enrichment_details["arxiv_comment"] = canonical_comment
    workflow_tags = [
        tag
        for tag in session_tags
        if tag.casefold().startswith((STATUS_TAG_PREFIX, PRIORITY_TAG_PREFIX))
    ]
    if workflow_tags:
        enrichment_details["workflow_tags"] = workflow_tags
    title = item["title"]
    source_url = source_url or item.get("url") or "https://example.invalid/"
    if download_timeout <= 0 or verify_timeout <= 0:
        raise ImportFailure("invalid_timeout", "Download and verification timeouts must be positive")

    if context is None:
        context = prepare_context(base_url, collection, target_id)
    else:
        if target_id != context.target_id:
            raise ImportFailure("context_mismatch", "Prepared Zotero context has a different target ID")
        context = revalidate_context(context, base_url, collection)

    if dry_run:
        possible_duplicate_keys = candidate_keys(matching_item_candidates(base_url, item))
        return {
            "status": "ready",
            "title": title,
            "identity": item_identity(item),
            "collection": context.target["name"],
            "target_id": context.target["id"],
            "zotero_version": context.zotero_version,
            **enrichment_details,
            "possible_duplicate_count": len(possible_duplicate_keys),
            "possible_duplicate_keys": possible_duplicate_keys,
        }
    if bool(pdf_file) == bool(pdf_url):
        raise ImportFailure("pdf_required", "Pass exactly one of --pdf-file or --pdf-url")

    pdf, recorded_pdf_url = read_pdf_source(
        pdf_file=pdf_file,
        pdf_url=pdf_url,
        pdf_source_url=pdf_source_url,
        source_url=source_url,
        referrer=referrer,
        download_timeout=download_timeout,
    )

    # A PDF download may take long enough for the selected collection to change.
    # Re-resolve it immediately before the first persistent write.
    context = revalidate_context(context, base_url, collection)
    preexisting_candidates = matching_item_candidates(base_url, item)
    possible_duplicate_keys = candidate_keys(preexisting_candidates)
    possible_duplicate_details = {
        "possible_duplicate_count": len(possible_duplicate_keys),
        "possible_duplicate_keys": possible_duplicate_keys,
    }

    session_id = "codex-" + uuid.uuid4().hex
    connector_item_id = uuid.uuid4().hex[:8].upper()
    item["id"] = connector_item_id

    save_payload = {"sessionID": session_id, "uri": source_url, "items": [item]}
    status, _, _ = connector_post(base_url, "/connector/saveItems", save_payload)
    if status != 201:
        raise ImportFailure("metadata_save_failed", f"Zotero returned HTTP {status} while saving metadata")

    try:
        status, _, _ = connector_post(
            base_url,
            "/connector/updateSession",
            {"sessionID": session_id, "target": context.target["id"], "tags": session_tags},
        )
        if status != 200:
            raise ImportFailure("collection_update_failed", f"Zotero returned HTTP {status} while setting the collection")
        upload_pdf(base_url, session_id, connector_item_id, pdf, recorded_pdf_url, pdf_title)
        verified = verify_new_item(
            base_url,
            item,
            context.collection_key,
            verify_timeout,
            set(possible_duplicate_keys),
        )
    except ImportFailure as error:
        error.details.setdefault("metadata_saved", True)
        error.details.setdefault("identity", item_identity(item))
        error.details.setdefault("possible_duplicate_count", len(possible_duplicate_keys))
        error.details.setdefault("possible_duplicate_keys", possible_duplicate_keys)
        raise

    return {
        "status": "saved_with_pdf",
        "title": title,
        "identity": item_identity(item),
        "collection": context.target["name"],
        "target_id": context.target["id"],
        "zotero_version": context.zotero_version,
        **enrichment_details,
        **possible_duplicate_details,
        **verified,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--item-json", type=Path, required=True, help="UTF-8 JSON file containing one Zotero item")
    parser.add_argument("--collection", required=True, help="Exact target collection name")
    parser.add_argument("--target-id", help="Connector tree target ID, used only when collection names are ambiguous")
    pdf_group = parser.add_mutually_exclusive_group()
    pdf_group.add_argument("--pdf-file", type=Path, help="Existing PDF file to attach")
    pdf_group.add_argument("--pdf-url", help="Public PDF URL to download and attach")
    parser.add_argument("--pdf-source-url", help="Source URL recorded on the attachment")
    parser.add_argument("--pdf-title", default="PDF", help="Attachment title shown in Zotero")
    parser.add_argument("--source-url", help="Landing-page URL recorded as the Connector request URI")
    parser.add_argument("--referrer", help="HTTP Referer used only when downloading --pdf-url")
    parser.add_argument("--tag", action="append", default=[], help="Tag to add; repeat for multiple tags")
    parser.add_argument("--note", action="append", default=[], help="Child note HTML/text to add; repeat for multiple notes")
    parser.add_argument("--arxiv-comment", help="arXiv Comments text saved as a 'Comment: ...' child note")
    parser.add_argument(
        "--reading-status",
        choices=sorted(READING_STATUS_OPTIONS),
        help="Ethereal Style reading status; defaults to to-read, use none to opt out",
    )
    parser.add_argument("--priority", choices=sorted(PRIORITIES), help="Ethereal Style priority")
    parser.add_argument("--download-timeout", type=float, default=60)
    parser.add_argument("--verify-timeout", type=float, default=30)
    parser.add_argument("--dry-run", action="store_true", help="Check inputs and report possible duplicates without writing")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    return parser


def run(args: argparse.Namespace) -> dict:
    return import_item(
        item=load_item(args.item_json),
        collection=args.collection,
        target_id=args.target_id,
        pdf_file=args.pdf_file,
        pdf_url=args.pdf_url,
        pdf_source_url=args.pdf_source_url,
        pdf_title=args.pdf_title,
        source_url=args.source_url,
        referrer=args.referrer,
        notes=args.note,
        tags=args.tag,
        arxiv_comment=args.arxiv_comment,
        reading_status=args.reading_status,
        priority=args.priority,
        download_timeout=args.download_timeout,
        verify_timeout=args.verify_timeout,
        dry_run=args.dry_run,
        base_url=args.base_url,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run(args)
    except ImportFailure as error:
        result = {"status": error.status, "message": error.message, **error.details}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return error.exit_code
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
