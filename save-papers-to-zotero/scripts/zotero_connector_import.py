#!/usr/bin/env python3
"""Import one paper and its PDF through Zotero's local Connector server."""

from __future__ import annotations

import argparse
import copy
import hashlib
import http.client
import json
import os
import random
import re
import socket
import ssl
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, TypedDict


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
SAFETY_LEVELS = {"fast", "balanced", "strict"}
DEFAULT_SAFETY_LEVEL = "balanced"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024
PDF_TAIL_BYTES = 4096
TRANSIENT_HTTP_STATUSES = {408, 425, 429, 500, 502, 503, 504}
ZOTERO_READ_ATTEMPTS = 3
ZOTERO_READ_RETRY_BACKOFF = 0.15
PDF_SOURCE_FALLBACK_STATUSES = {
    "invalid_pdf",
    "pdf_read_failed",
    "pdf_http_error",
    "pdf_download_incomplete",
    "pdf_download_timeout",
    "pdf_download_tls_error",
    "pdf_source_connection_error",
}

ImportStatus = Literal["ready", "saved_with_pdf"]


class CandidateInspection(TypedDict):
    item_key: str
    title: str | None
    in_target_collection: bool
    pdf_count: int
    pdf_verified: bool
    pdf_files: list[dict]


def configure_utf8_stdio() -> None:
    """Make CLI JSON output independent of the Windows console code page."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


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
    pdf_size: int
    pdf_sha256: str
    pdf_download_attempts: int


@dataclass
class ImportContext:
    base_url: str
    collection: str
    target_id: str | None
    target: dict
    collection_key: str
    zotero_version: str | None
    prewrite_confirmed: bool = False


@dataclass
class PreparedPDF:
    path: Path
    source_url: str
    size: int
    sha256: str
    download_attempts: int
    temporary: bool = False
    title: str = "PDF"
    source_index: int = 0
    source_attempts: list[dict] = field(default_factory=list)

    def cleanup(self) -> None:
        if not self.temporary:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass


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
    body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    connection_status: str = "zotero_connection_error",
    http_error_status: str = "zotero_http_error",
    stage: str = "zotero_request",
) -> tuple[int, bytes, object]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        try:
            payload = error.read()
        except OSError:
            payload = b""
        text = payload.decode("utf-8", errors="replace")
        raise ImportFailure(
            http_error_status,
            f"{method} {urllib.parse.urlsplit(url).path} returned HTTP {error.code}: {text[:300]}",
            failure_stage=stage,
            source_url=url,
            http_status=error.code,
        ) from error
    except urllib.error.URLError as error:
        raise ImportFailure(
            connection_status,
            f"Cannot reach {url}: {error.reason}",
            failure_stage=stage,
            source_url=url,
        ) from error
    except (http.client.IncompleteRead, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as error:
        raise ImportFailure(
            connection_status,
            f"Connection failed while accessing {url}: {error}",
            failure_stage=stage,
            source_url=url,
        ) from error


def connector_post(base_url: str, path: str, payload: dict) -> tuple[int, bytes, object]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return request_bytes(
        base_url + path,
        method="POST",
        body=body,
        headers=CONNECTOR_HEADERS,
    )


def transient_zotero_failure(error: ImportFailure) -> bool:
    if error.status == "zotero_connection_error":
        return True
    return error.status == "zotero_http_error" and error.details.get("http_status") in TRANSIENT_HTTP_STATUSES


def retry_zotero_read(operation: Callable[[], object]) -> object:
    last_error: ImportFailure | None = None
    for attempt in range(1, ZOTERO_READ_ATTEMPTS + 1):
        try:
            return operation()
        except ImportFailure as error:
            last_error = error
            if not transient_zotero_failure(error) or attempt >= ZOTERO_READ_ATTEMPTS:
                error.details.setdefault("attempts", attempt)
                raise
            time.sleep(ZOTERO_READ_RETRY_BACKOFF * (2 ** (attempt - 1)))
    assert last_error is not None
    raise last_error


def connector_read_post(base_url: str, path: str, payload: dict) -> tuple[int, bytes, object]:
    result = retry_zotero_read(lambda: connector_post(base_url, path, payload))
    assert isinstance(result, tuple)
    return result


def api_get(base_url: str, path: str) -> object:
    result = retry_zotero_read(
        lambda: request_bytes(
            base_url + path,
            headers={"Zotero-API-Version": "3", "Accept": "application/json"},
        )
    )
    assert isinstance(result, tuple)
    _, payload, _ = result
    return decode_json_response(payload, base_url + path)


def decode_json_response(payload: bytes, source: str) -> object:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ImportFailure(
            "invalid_api_response",
            f"Zotero returned invalid JSON from {source}: {error}",
            failure_stage="zotero_response",
            source_url=source,
        ) from error


def data_object(wrapper: dict) -> dict:
    return wrapper.get("data", wrapper)


def wrapper_key(wrapper: dict) -> str | None:
    return wrapper.get("key") or data_object(wrapper).get("key")


def find_target(base_url: str, collection_name: str, explicit_target_id: str | None) -> dict:
    _, payload, _ = connector_read_post(base_url, "/connector/getSelectedCollection", {})
    response = decode_json_response(payload, base_url + "/connector/getSelectedCollection")
    if not isinstance(response, dict):
        raise ImportFailure(
            "invalid_api_response",
            "Zotero target response must be a JSON object",
            failure_stage="zotero_response",
            source_url=base_url + "/connector/getSelectedCollection",
        )
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
            raise ImportFailure(
                "target_not_found",
                f"Collection {collection_name!r} was not found in Zotero. The local Zotero server cannot create collections through its API; create this collection manually in the Zotero app, then rerun the import.",
            )
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
        page = api_get(
            base_url,
            f"/api/users/0/collections?format=json&include=data&limit=100&start={start}",
        )
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
            if error.status == "zotero_http_error" and "HTTP 403" in error.message:
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


def attachment_file_path(base_url: str, attachment_key: str) -> Path | None:
    try:
        _, payload, _ = request_bytes(
            f"{base_url}/api/users/0/items/{attachment_key}/file/view/url",
            headers={"Zotero-API-Version": "3"},
        )
    except ImportFailure as error:
        if error.status == "zotero_http_error" and error.details.get("http_status") == 404:
            return None
        raise
    value = payload.decode("utf-8", errors="replace").strip()
    if not value.startswith("file:"):
        return None
    parsed = urllib.parse.urlparse(value)
    path = urllib.request.url2pathname(urllib.parse.unquote(parsed.path))
    if os.name == "nt" and path.startswith(("/", "\\")) and len(path) > 2 and path[2] == ":":
        path = path[1:]
    candidate = Path(path)
    return candidate if candidate.is_file() else None


def file_fingerprint(path: Path, expected_size: int) -> tuple[int, str | None]:
    digest = hashlib.sha256()
    try:
        size = path.stat().st_size
        if size != expected_size:
            return size, None
        measured_size = 0
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                measured_size += len(chunk)
                digest.update(chunk)
    except OSError as error:
        raise ImportFailure(
            "verification_file_read_failed",
            f"Cannot read Zotero's stored PDF during verification: {error}",
            failure_stage="post_write_verification",
            stored_file=str(path),
        ) from error
    return measured_size, digest.hexdigest()


def inspect_candidate(
    base_url: str,
    wrapper: dict,
    target_collection_key: str,
    expected_pdf: PreparedPDF | None = None,
) -> CandidateInspection:
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
        file_path = attachment_file_path(base_url, key) if key else None
        pdf_info = {
            "key": key,
            "title": child_data.get("title"),
            "file_exists": file_path is not None,
        }
        if file_path is not None and expected_pdf is not None:
            file_size, file_sha256 = file_fingerprint(file_path, expected_pdf.size)
            pdf_info.update(
                {
                    "file_size": file_size,
                    "file_sha256": file_sha256,
                    "content_matches": file_size == expected_pdf.size and file_sha256 == expected_pdf.sha256,
                }
            )
        pdfs.append(pdf_info)
    collections = data.get("collections", [])
    return {
        "item_key": item_key,
        "title": data.get("title"),
        "in_target_collection": target_collection_key in collections,
        "pdf_count": len(pdfs),
        "pdf_verified": any(
            pdf.get("content_matches", pdf["file_exists"] and expected_pdf is None)
            for pdf in pdfs
        ),
        "pdf_files": pdfs,
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


def inspect_pdf_file(
    path: Path,
    *,
    source_url: str,
    download_attempts: int,
    temporary: bool,
) -> PreparedPDF:
    digest = hashlib.sha256()
    size = 0
    signature = b""
    tail = b""
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                if not signature:
                    signature = chunk[:5]
                size += len(chunk)
                digest.update(chunk)
                tail = (tail + chunk)[-PDF_TAIL_BYTES:]
    except OSError as error:
        raise ImportFailure(
            "pdf_read_failed",
            f"Cannot read PDF file: {error}",
            failure_stage="pdf_read",
            source_url=source_url,
        ) from error
    if not signature.startswith(b"%PDF-"):
        raise ImportFailure(
            "invalid_pdf",
            "The supplied attachment does not begin with a PDF signature",
            failure_stage="pdf_validation",
            source_url=source_url,
            downloaded_bytes=size,
            attempts=download_attempts,
        )
    if not tail.rstrip().endswith(b"%%EOF"):
        raise ImportFailure(
            "invalid_pdf",
            "The supplied attachment does not end with a PDF EOF marker",
            failure_stage="pdf_validation",
            source_url=source_url,
            downloaded_bytes=size,
            attempts=download_attempts,
        )
    return PreparedPDF(
        path=path,
        source_url=source_url,
        size=size,
        sha256=digest.hexdigest(),
        download_attempts=download_attempts,
        temporary=temporary,
    )


def set_response_read_timeout(response: object, timeout: float) -> None:
    """Best-effort split of urllib's connect and socket read timeouts."""
    try:
        response.fp.raw._sock.settimeout(timeout)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def download_failure(
    error: BaseException,
    *,
    url: str,
    attempt: int,
    downloaded_bytes: int,
    expected_bytes: int | None,
) -> ImportFailure:
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    details = {
        "failure_stage": "pdf_download",
        "source_url": url,
        "attempts": attempt,
        "downloaded_bytes": downloaded_bytes,
    }
    if expected_bytes is not None:
        details["expected_bytes"] = expected_bytes
    if isinstance(reason, http.client.IncompleteRead):
        status = "pdf_download_incomplete"
    elif isinstance(reason, (TimeoutError, socket.timeout)):
        status = "pdf_download_timeout"
    elif isinstance(reason, ssl.SSLError):
        status = "pdf_download_tls_error"
    else:
        status = "pdf_source_connection_error"
    return ImportFailure(status, f"PDF download failed from {url} on attempt {attempt}: {reason}", **details)


def retry_delay(attempt: int, base: float, deadline: float) -> None:
    if base <= 0:
        return
    delay = base * (2 ** (attempt - 1))
    delay += random.uniform(0, min(delay * 0.25, 1.0))
    remaining = deadline - time.monotonic()
    if remaining > 0:
        time.sleep(min(delay, remaining))


def download_pdf_to_temp(
    url: str,
    *,
    referrer: str | None,
    connect_timeout: float,
    read_timeout: float,
    max_attempts: int,
    retry_backoff: float,
    wall_timeout: float,
    progress: Callable[[dict], None] | None = None,
) -> PreparedPDF:
    headers = {"User-Agent": "Mozilla/5.0 Zotero-Automation/1.0", "Accept": "application/pdf"}
    if referrer:
        headers["Referer"] = referrer
    deadline = time.monotonic() + wall_timeout
    last_failure: ImportFailure | None = None
    attempts_made = 0

    for attempt in range(1, max_attempts + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        attempts_made = attempt
        if progress:
            progress({"event": "pdf_download_attempt", "source_url": url, "attempt": attempt})
        try:
            descriptor, temp_name = tempfile.mkstemp(prefix="zotero-pdf-", suffix=".pdf")
        except OSError as error:
            raise ImportFailure(
                "pdf_tempfile_failed",
                f"Cannot create a temporary PDF file: {error}",
                failure_stage="pdf_download",
                source_url=url,
                attempts=attempt,
            ) from error
        os.close(descriptor)
        temp_path = Path(temp_name)
        downloaded_bytes = 0
        expected_bytes: int | None = None
        keep_file = False
        try:
            request = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(request, timeout=min(connect_timeout, remaining)) as response:
                set_response_read_timeout(response, min(read_timeout, max(0.001, deadline - time.monotonic())))
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        parsed_length = int(content_length)
                        expected_bytes = parsed_length if parsed_length >= 0 else None
                    except ValueError:
                        expected_bytes = None
                digest = hashlib.sha256()
                signature = b""
                tail = b""
                last_progress = time.monotonic()
                try:
                    output = temp_path.open("wb")
                except OSError as error:
                    raise ImportFailure(
                        "pdf_temp_io_error",
                        f"Cannot open the temporary PDF file for writing: {error}",
                        failure_stage="pdf_temp_write",
                        source_url=url,
                        attempts=attempt,
                    ) from error
                with output:
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("per-paper PDF download wall timeout exceeded")
                        set_response_read_timeout(response, min(read_timeout, max(0.001, remaining)))
                        read_once = getattr(response, "read1", response.read)
                        chunk = read_once(DOWNLOAD_CHUNK_SIZE)
                        if time.monotonic() >= deadline:
                            raise TimeoutError("per-paper PDF download wall timeout exceeded")
                        if not chunk:
                            break
                        if not signature:
                            signature = chunk[:5]
                        try:
                            output.write(chunk)
                        except OSError as error:
                            raise ImportFailure(
                                "pdf_temp_io_error",
                                f"Cannot write the temporary PDF file: {error}",
                                failure_stage="pdf_temp_write",
                                source_url=url,
                                attempts=attempt,
                                downloaded_bytes=downloaded_bytes,
                            ) from error
                        digest.update(chunk)
                        downloaded_bytes += len(chunk)
                        tail = (tail + chunk)[-PDF_TAIL_BYTES:]
                        now = time.monotonic()
                        if progress and now - last_progress >= 1:
                            progress(
                                {
                                    "event": "pdf_download_progress",
                                    "source_url": url,
                                    "attempt": attempt,
                                    "downloaded_bytes": downloaded_bytes,
                                    "expected_bytes": expected_bytes,
                                }
                            )
                            last_progress = now
                    try:
                        output.flush()
                        os.fsync(output.fileno())
                    except OSError as error:
                        raise ImportFailure(
                            "pdf_temp_io_error",
                            f"Cannot finalize the temporary PDF file: {error}",
                            failure_stage="pdf_temp_write",
                            source_url=url,
                            attempts=attempt,
                            downloaded_bytes=downloaded_bytes,
                        ) from error
                if expected_bytes is not None and downloaded_bytes != expected_bytes:
                    raise http.client.IncompleteRead(b"", abs(expected_bytes - downloaded_bytes))
                if not signature.startswith(b"%PDF-"):
                    raise ImportFailure(
                        "invalid_pdf",
                        "The downloaded attachment does not begin with a PDF signature",
                        failure_stage="pdf_validation",
                        source_url=url,
                        attempts=attempt,
                        downloaded_bytes=downloaded_bytes,
                    )
                if not tail.rstrip().endswith(b"%%EOF"):
                    raise ImportFailure(
                        "invalid_pdf",
                        "The downloaded attachment does not end with a PDF EOF marker",
                        failure_stage="pdf_validation",
                        source_url=url,
                        attempts=attempt,
                        downloaded_bytes=downloaded_bytes,
                    )
                if progress:
                    progress(
                        {
                            "event": "pdf_download_complete",
                            "source_url": url,
                            "attempt": attempt,
                            "downloaded_bytes": downloaded_bytes,
                            "expected_bytes": expected_bytes,
                        }
                    )
                keep_file = True
                return PreparedPDF(
                    path=temp_path,
                    source_url=url,
                    size=downloaded_bytes,
                    sha256=digest.hexdigest(),
                    download_attempts=attempt,
                    temporary=True,
                )
        except urllib.error.HTTPError as error:
            last_failure = ImportFailure(
                "pdf_http_error",
                f"PDF source {url} returned HTTP {error.code}",
                failure_stage="pdf_download",
                source_url=url,
                http_status=error.code,
                attempts=attempt,
                downloaded_bytes=downloaded_bytes,
            )
            if error.code not in TRANSIENT_HTTP_STATUSES:
                raise last_failure from error
        except ImportFailure:
            raise
        except (
            urllib.error.URLError,
            http.client.IncompleteRead,
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            ConnectionError,
            OSError,
        ) as error:
            last_failure = download_failure(
                error,
                url=url,
                attempt=attempt,
                downloaded_bytes=downloaded_bytes,
                expected_bytes=expected_bytes,
            )
        finally:
            if not keep_file:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass

        if attempt < max_attempts:
            retry_delay(attempt, retry_backoff, deadline)

    if time.monotonic() >= deadline:
        details = dict(last_failure.details) if last_failure is not None else {}
        details.update(
            {
                "failure_stage": "pdf_download",
                "source_url": url,
                "attempts": attempts_made,
            }
        )
        raise ImportFailure(
            "pdf_download_timeout",
            f"PDF download from {url} exceeded the {wall_timeout:g}s per-paper wall timeout",
            **details,
        ) from last_failure
    if last_failure is not None:
        raise last_failure
    raise ImportFailure(
        "pdf_source_connection_error",
        f"PDF download failed from {url}",
        failure_stage="pdf_download",
        source_url=url,
        attempts=attempts_made,
    )


def read_pdf_source(
    *,
    pdf_file: Path | None,
    pdf_url: str | None,
    pdf_source_url: str | None,
    source_url: str,
    referrer: str | None,
    download_timeout: float,
    connect_timeout: float = 15,
    download_attempts: int = 3,
    retry_backoff: float = 0.5,
    per_paper_wall_timeout: float = 300,
    progress: Callable[[dict], None] | None = None,
) -> PreparedPDF:
    if pdf_file:
        recorded_source = pdf_source_url or source_url
        return inspect_pdf_file(
            Path(pdf_file),
            source_url=recorded_source,
            download_attempts=1,
            temporary=False,
        )
    prepared = download_pdf_to_temp(
        str(pdf_url),
        referrer=referrer,
        connect_timeout=connect_timeout,
        read_timeout=download_timeout,
        max_attempts=download_attempts,
        retry_backoff=retry_backoff,
        wall_timeout=per_paper_wall_timeout,
        progress=progress,
    )
    prepared.source_url = pdf_source_url or str(pdf_url)
    return prepared


def read_pdf_sources(
    sources: list[dict],
    *,
    default_source_url: str,
    default_title: str,
    default_referrer: str | None,
    download_timeout: float,
    connect_timeout: float,
    download_attempts: int,
    retry_backoff: float,
    per_paper_wall_timeout: float,
    progress: Callable[[dict], None] | None = None,
) -> PreparedPDF:
    if not sources:
        raise ImportFailure("pdf_required", "Provide at least one PDF source")
    started = time.monotonic()
    source_attempts: list[dict] = []
    last_failure: ImportFailure | None = None
    for source_index, source in enumerate(sources):
        remaining = per_paper_wall_timeout - (time.monotonic() - started)
        if remaining <= 0:
            break
        source_url = source.get("pdf_source_url") or source.get("pdf_url") or default_source_url
        if progress:
            progress(
                {
                    "event": "pdf_source_started",
                    "source_index": source_index,
                    "source_url": str(source_url),
                }
            )
        try:
            prepared = read_pdf_source(
                pdf_file=source.get("pdf_file"),
                pdf_url=source.get("pdf_url"),
                pdf_source_url=source.get("pdf_source_url"),
                source_url=default_source_url,
                referrer=source.get("referrer", default_referrer),
                download_timeout=download_timeout,
                connect_timeout=connect_timeout,
                download_attempts=download_attempts,
                retry_backoff=retry_backoff,
                per_paper_wall_timeout=remaining,
                progress=progress,
            )
        except ImportFailure as error:
            last_failure = error
            source_attempts.append(
                {
                    "source_index": source_index,
                    "source_url": str(source_url),
                    "status": error.status,
                    "message": error.message,
                    **error.details,
                }
            )
            if progress:
                progress(
                    {
                        "event": "pdf_source_failed",
                        "source_index": source_index,
                        "source_url": str(source_url),
                        "status": error.status,
                    }
                )
            if error.status not in PDF_SOURCE_FALLBACK_STATUSES:
                error.details.setdefault("pdf_source_attempts", source_attempts)
                raise
            continue
        prepared.title = source.get("pdf_title") or default_title
        prepared.source_index = source_index
        source_attempts.append(
            {
                "source_index": source_index,
                "source_url": prepared.source_url,
                "status": "selected",
                "attempts": prepared.download_attempts,
                "downloaded_bytes": prepared.size,
                "pdf_sha256": prepared.sha256,
            }
        )
        prepared.source_attempts = source_attempts
        return prepared

    if len(sources) == 1 and last_failure is not None:
        last_failure.details.setdefault("pdf_source_attempts", source_attempts)
        raise last_failure
    raise ImportFailure(
        "pdf_sources_exhausted",
        f"All {len(sources)} PDF sources failed",
        failure_stage="pdf_source_selection",
        attempts=len(source_attempts),
        pdf_source_attempts=source_attempts,
    ) from last_failure


def upload_pdf(
    base_url: str,
    session_id: str,
    parent_item_id: str,
    pdf: PreparedPDF,
    title: str,
) -> None:
    metadata = json.dumps(
        {
            "sessionID": session_id,
            "title": title,
            "parentItemID": parent_item_id,
            "url": pdf.source_url,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    try:
        with pdf.path.open("rb") as handle:
            status, _, _ = request_bytes(
                base_url + "/connector/saveAttachment",
                method="POST",
                body=handle,
                headers={
                    "Content-Type": "application/pdf",
                    "Content-Length": str(pdf.size),
                    "X-Zotero-Connector-API-Version": "3",
                    "X-Metadata": metadata,
                },
                timeout=60,
                stage="pdf_upload",
            )
    except OSError as error:
        raise ImportFailure(
            "pdf_read_failed",
            f"Cannot stream PDF to Zotero: {error}",
            failure_stage="pdf_upload",
            source_url=pdf.source_url,
        ) from error
    if status != 201:
        raise ImportFailure("attachment_save_failed", f"Zotero returned HTTP {status} while saving the PDF")


def verify_new_item(
    base_url: str,
    item: dict,
    target_collection_key: str,
    timeout: float,
    preexisting_keys: set[str],
    expected_pdf: PreparedPDF,
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
            inspected = [
                inspect_candidate(base_url, candidate, target_collection_key, expected_pdf)
                for candidate in candidates
            ]
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
        expected_pdf_size=expected_pdf.size,
        expected_pdf_sha256=expected_pdf.sha256,
    )


def prepare_context(base_url: str, collection: str, target_id: str | None = None) -> ImportContext:
    status, _, response_headers = connector_read_post(base_url, "/connector/ping", {})
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


def confirm_context(context: ImportContext, base_url: str, collection: str) -> ImportContext:
    """Lightly confirm the prepared target once, without re-listing all collections."""
    if context.base_url != base_url or normalized(context.collection) != normalized(collection):
        raise ImportFailure("context_mismatch", "Prepared Zotero context does not match this import request")
    status, _, response_headers = connector_read_post(base_url, "/connector/ping", {})
    if status != 200:
        raise ImportFailure("zotero_unavailable", f"Zotero ping returned HTTP {status}")
    fresh_target = find_target(base_url, collection, context.target_id)
    if (
        fresh_target.get("id") != context.target.get("id")
        or normalized(fresh_target.get("name", "")) != normalized(context.target.get("name", ""))
    ):
        raise ImportFailure(
            "context_changed",
            f"Zotero target {collection!r} changed during the import; stopping before another write",
        )
    context.target = fresh_target
    context.zotero_version = response_headers.get("X-Zotero-Version") or context.zotero_version
    context.prewrite_confirmed = True
    return context


def import_item(
    *,
    item: dict,
    collection: str,
    target_id: str | None = None,
    pdf_file: Path | None = None,
    pdf_url: str | None = None,
    pdf_sources: list[dict] | None = None,
    pdf_source_url: str | None = None,
    pdf_title: str = "PDF",
    source_url: str | None = None,
    referrer: str | None = None,
    notes: list[str] | None = None,
    tags: list[str] | None = None,
    arxiv_comment: str | None = None,
    reading_status: str | None = None,
    priority: str | None = None,
    safety_level: str = DEFAULT_SAFETY_LEVEL,
    connect_timeout: float = 15,
    download_timeout: float = 60,
    download_attempts: int = 3,
    retry_backoff: float = 0.5,
    per_paper_wall_timeout: float = 300,
    verify_timeout: float = 30,
    dry_run: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    context: ImportContext | None = None,
    progress_callback: Callable[[dict], None] | None = None,
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
    safety_level = canonical_workflow_value(safety_level, "safety_level", SAFETY_LEVELS)
    if connect_timeout <= 0 or download_timeout <= 0 or per_paper_wall_timeout <= 0 or verify_timeout <= 0:
        raise ImportFailure("invalid_timeout", "Connection, download, wall, and verification timeouts must be positive")
    if not isinstance(download_attempts, int) or download_attempts < 1:
        raise ImportFailure("invalid_download_attempts", "Download attempts must be a positive integer")
    if retry_backoff < 0:
        raise ImportFailure("invalid_retry_backoff", "Download retry backoff cannot be negative")

    if context is None:
        context = prepare_context(base_url, collection, target_id)
    else:
        if target_id != context.target_id:
            raise ImportFailure("context_mismatch", "Prepared Zotero context has a different target ID")
        if context.base_url != base_url or normalized(context.collection) != normalized(collection):
            raise ImportFailure("context_mismatch", "Prepared Zotero context does not match this import request")
        if safety_level == "strict":
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
    if pdf_sources is not None and (pdf_file is not None or pdf_url is not None):
        raise ImportFailure("pdf_required", "Use either pdf_sources or one legacy PDF source, not both")
    sources = pdf_sources
    if sources is None:
        if bool(pdf_file) == bool(pdf_url):
            raise ImportFailure("pdf_required", "Pass exactly one of --pdf-file or --pdf-url")
        sources = [
            {
                "pdf_file": pdf_file,
                "pdf_url": pdf_url,
                "pdf_source_url": pdf_source_url,
                "pdf_title": pdf_title,
                "referrer": referrer,
            }
        ]
    if not isinstance(sources, list) or not sources or any(not isinstance(source, dict) for source in sources):
        raise ImportFailure("pdf_required", "pdf_sources must be a non-empty array of source objects")
    for source_index, source in enumerate(sources):
        if bool(source.get("pdf_file")) == bool(source.get("pdf_url")):
            raise ImportFailure(
                "pdf_required",
                f"PDF source {source_index} requires exactly one of pdf_file or pdf_url",
            )

    pdf = read_pdf_sources(
        sources,
        default_source_url=source_url,
        default_title=pdf_title,
        default_referrer=referrer,
        download_timeout=download_timeout,
        connect_timeout=connect_timeout,
        download_attempts=download_attempts,
        retry_backoff=retry_backoff,
        per_paper_wall_timeout=per_paper_wall_timeout,
        progress=progress_callback,
    )
    metadata_saved = False
    possible_duplicate_keys: list[str] = []
    try:
        if progress_callback:
            progress_callback({"event": "validating_target"})
        if safety_level == "strict":
            context = revalidate_context(context, base_url, collection)
        elif safety_level == "balanced" and not context.prewrite_confirmed:
            context = confirm_context(context, base_url, collection)

        preexisting_candidates = matching_item_candidates(base_url, item)
        possible_duplicate_keys = candidate_keys(preexisting_candidates)
        possible_duplicate_details = {
            "possible_duplicate_count": len(possible_duplicate_keys),
            "possible_duplicate_keys": possible_duplicate_keys,
        }

        session_id = "zotero-import-" + uuid.uuid4().hex
        connector_item_id = uuid.uuid4().hex[:8].upper()
        item["id"] = connector_item_id

        save_payload = {"sessionID": session_id, "uri": source_url, "items": [item]}
        if progress_callback:
            progress_callback({"event": "saving_metadata"})
        status, _, _ = connector_post(base_url, "/connector/saveItems", save_payload)
        if status != 201:
            raise ImportFailure("metadata_save_failed", f"Zotero returned HTTP {status} while saving metadata")
        metadata_saved = True

        if progress_callback:
            progress_callback({"event": "assigning_collection"})
        status, _, _ = connector_post(
            base_url,
            "/connector/updateSession",
            {"sessionID": session_id, "target": context.target["id"], "tags": session_tags},
        )
        if status != 200:
            raise ImportFailure("collection_update_failed", f"Zotero returned HTTP {status} while setting the collection")
        if progress_callback:
            progress_callback(
                {
                    "event": "uploading_attachment",
                    "pdf_bytes": pdf.size,
                    "source_url": pdf.source_url,
                }
            )
        upload_pdf(base_url, session_id, connector_item_id, pdf, pdf.title)
        if progress_callback:
            progress_callback({"event": "verifying_stored_file", "expected_pdf_bytes": pdf.size})
        verified = verify_new_item(
            base_url,
            item,
            context.collection_key,
            verify_timeout,
            set(possible_duplicate_keys),
            pdf,
        )

        return {
            "status": "saved_with_pdf",
            "title": title,
            "identity": item_identity(item),
            "collection": context.target["name"],
            "target_id": context.target["id"],
            "zotero_version": context.zotero_version,
            **enrichment_details,
            **possible_duplicate_details,
            "pdf_size": pdf.size,
            "pdf_sha256": pdf.sha256,
            "pdf_download_attempts": pdf.download_attempts,
            "pdf_source_index": pdf.source_index,
            "pdf_source_url": pdf.source_url,
            "pdf_source_attempts": pdf.source_attempts,
            **verified,
        }
    except ImportFailure as error:
        if metadata_saved:
            error.details.setdefault("metadata_saved", True)
        error.details.setdefault("identity", item_identity(item))
        error.details.setdefault("possible_duplicate_count", len(possible_duplicate_keys))
        error.details.setdefault("possible_duplicate_keys", possible_duplicate_keys)
        error.details.setdefault("pdf_size", pdf.size)
        error.details.setdefault("pdf_sha256", pdf.sha256)
        error.details.setdefault("pdf_download_attempts", pdf.download_attempts)
        error.details.setdefault("pdf_source_index", pdf.source_index)
        error.details.setdefault("pdf_source_url", pdf.source_url)
        error.details.setdefault("pdf_source_attempts", pdf.source_attempts)
        raise
    finally:
        pdf.cleanup()


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
    parser.add_argument(
        "--safety-level",
        choices=sorted(SAFETY_LEVELS),
        default=DEFAULT_SAFETY_LEVEL,
        help="Target-checking policy; balanced confirms once before the write",
    )
    parser.add_argument("--connect-timeout", type=float, default=15, help="PDF connection timeout in seconds")
    parser.add_argument("--download-timeout", type=float, default=60, help="PDF socket read timeout in seconds")
    parser.add_argument("--download-attempts", type=int, default=3, help="Maximum PDF download attempts")
    parser.add_argument("--retry-backoff", type=float, default=0.5, help="Initial exponential retry delay in seconds")
    parser.add_argument(
        "--per-paper-wall-timeout",
        type=float,
        default=300,
        help="Overall PDF download time budget per paper in seconds",
    )
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
        safety_level=args.safety_level,
        connect_timeout=args.connect_timeout,
        download_timeout=args.download_timeout,
        download_attempts=args.download_attempts,
        retry_backoff=args.retry_backoff,
        per_paper_wall_timeout=args.per_paper_wall_timeout,
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
    except Exception as error:
        result = {
            "status": "internal_error",
            "message": f"Unexpected {type(error).__name__}: {error}",
            "failure_stage": "single_import",
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 70
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    configure_utf8_stdio()
    sys.exit(main())
