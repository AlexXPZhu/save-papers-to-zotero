#!/usr/bin/env python3
"""Import a manifest of papers sequentially through Zotero's local Connector server."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, TypedDict

import zotero_connector_import as single


SUCCESS_STATUSES = {
    "saved_with_pdf",
    "ready",
    "skipped_completed",
    "skipped_duplicate_in_manifest",
}
RESUMABLE_SUCCESS_STATUSES = {"saved_with_pdf", "skipped_completed"}
NEEDS_ACTION_STATUSES = {"verification_failed", "skipped_needs_review"}
FATAL_STATUSES = {
    "zotero_connection_error",
    "zotero_http_error",
    "zotero_unavailable",
    "local_api_disabled",
    "target_not_found",
    "target_ambiguous",
    "target_mismatch",
    "files_not_editable",
    "collection_verification_unavailable",
    "invalid_api_response",
    "context_mismatch",
    "context_changed",
    "collection_update_failed",
    "invalid_timeout",
    "pdf_tempfile_failed",
    "pdf_temp_io_error",
}
PAPER_FIELDS = {
    "id",
    "item",
    "item_json",
    "source_url",
    "pdf_file",
    "pdf_url",
    "pdf_sources",
    "pdf_source_url",
    "pdf_title",
    "referrer",
    "notes",
    "tags",
    "arxiv_comment",
    "reading_status",
    "priority",
}
PDF_SOURCE_FIELDS = {"pdf_file", "pdf_url", "pdf_source_url", "pdf_title", "referrer"}


class LedgerRecord(TypedDict, total=False):
    run_id: str
    timestamp: str
    index: int
    request_key: str
    title: str
    source_url: str | None
    status: str
    item_key: str
    possible_duplicate_count: int
    possible_duplicate_keys: list[str]
    message: str


class BatchSummary(TypedDict, total=False):
    status: str
    collection: str
    total: int
    completed: int
    needs_action: int
    failed_or_not_attempted: int
    possible_duplicate_items: int
    aborted: bool
    counts: dict[str, int]
    this_run_counts: dict[str, int]
    ledger: str
    results: list[LedgerRecord]
    task_results: list[LedgerRecord]
    historical_transient_failures: int
    historical_failure_items: list[dict]
    currently_unresolved: list[dict]
    resume_scope: dict
    safety_level: str


@dataclass
class BatchRequest:
    index: int
    request_key: str
    item: dict
    source_url: str | None
    pdf_file: Path | None
    pdf_url: str | None
    pdf_source_url: str | None
    pdf_title: str
    referrer: str | None
    notes: list[str]
    tags: list[str]
    arxiv_comment: str | None
    reading_status: str | None
    priority: str | None
    pdf_sources: list[dict]
    parse_error: single.ImportFailure | None = None


@dataclass
class LedgerLock:
    handle: BinaryIO
    path: Path


def load_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise single.ImportFailure("invalid_manifest", f"Cannot read batch manifest: {error}") from error


def string_list(value: object, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise single.ImportFailure("invalid_manifest", f"Manifest field {field!r} must be an array of non-empty strings")
    return [item.strip() for item in value]


def optional_string(value: object, field: str, default: str | None = None) -> str | None:
    if value is None:
        return default
    if not isinstance(value, str) or not value.strip():
        raise single.ImportFailure("invalid_manifest", f"Manifest field {field!r} must be a non-empty string")
    return value.strip()


def resolve_optional_path(value: object, manifest_dir: Path, field: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise single.ImportFailure("invalid_manifest", f"Manifest field {field!r} must be a non-empty path string")
    path = Path(value)
    return path if path.is_absolute() else manifest_dir / path


def parse_pdf_sources(
    entry: dict,
    *,
    index: int,
    manifest_dir: Path,
    require_pdf: bool,
    default_pdf_source_url: str | None,
    default_pdf_title: str,
    default_referrer: str | None,
) -> list[dict]:
    raw_sources = entry.get("pdf_sources")
    legacy_present = entry.get("pdf_file") is not None or entry.get("pdf_url") is not None
    if raw_sources is not None and legacy_present:
        raise single.ImportFailure(
            "invalid_manifest",
            f"Paper {index} cannot combine 'pdf_sources' with 'pdf_file' or 'pdf_url'",
        )
    if raw_sources is None:
        pdf_file = resolve_optional_path(entry.get("pdf_file"), manifest_dir, f"papers[{index}].pdf_file")
        pdf_url = optional_string(entry.get("pdf_url"), f"papers[{index}].pdf_url")
        if require_pdf and bool(pdf_file) == bool(pdf_url):
            raise single.ImportFailure(
                "invalid_manifest",
                f"Paper {index} requires exactly one of 'pdf_file', 'pdf_url', or 'pdf_sources'",
            )
        if not require_pdf and pdf_file and pdf_url:
            raise single.ImportFailure("invalid_manifest", f"Paper {index} cannot contain both 'pdf_file' and 'pdf_url'")
        if not pdf_file and not pdf_url:
            return []
        return [
            {
                "pdf_file": pdf_file,
                "pdf_url": pdf_url,
                "pdf_source_url": default_pdf_source_url,
                "pdf_title": default_pdf_title,
                "referrer": default_referrer,
            }
        ]

    if not isinstance(raw_sources, list) or (require_pdf and not raw_sources):
        raise single.ImportFailure(
            "invalid_manifest",
            f"Paper {index} field 'pdf_sources' must be a non-empty array",
        )
    sources: list[dict] = []
    for source_index, raw_source in enumerate(raw_sources):
        field = f"papers[{index}].pdf_sources[{source_index}]"
        if not isinstance(raw_source, dict):
            raise single.ImportFailure("invalid_manifest", f"Manifest field {field!r} must be an object")
        unknown = sorted(set(raw_source) - PDF_SOURCE_FIELDS)
        if unknown:
            raise single.ImportFailure(
                "invalid_manifest",
                f"Manifest field {field!r} contains unknown keys: {', '.join(unknown)}",
            )
        pdf_file = resolve_optional_path(raw_source.get("pdf_file"), manifest_dir, field + ".pdf_file")
        pdf_url = optional_string(raw_source.get("pdf_url"), field + ".pdf_url")
        if bool(pdf_file) == bool(pdf_url):
            raise single.ImportFailure(
                "invalid_manifest",
                f"Manifest field {field!r} requires exactly one of 'pdf_file' or 'pdf_url'",
            )
        sources.append(
            {
                "pdf_file": pdf_file,
                "pdf_url": pdf_url,
                "pdf_source_url": optional_string(
                    raw_source.get("pdf_source_url"),
                    field + ".pdf_source_url",
                    default_pdf_source_url,
                ),
                "pdf_title": optional_string(
                    raw_source.get("pdf_title"),
                    field + ".pdf_title",
                    default_pdf_title,
                ),
                "referrer": optional_string(
                    raw_source.get("referrer"),
                    field + ".referrer",
                    default_referrer,
                ),
            }
        )
    return sources


def invalid_batch_request(index: int, entry: object, error: single.ImportFailure) -> BatchRequest:
    entry_object = entry if isinstance(entry, dict) else {}
    item = entry_object.get("item") if isinstance(entry_object.get("item"), dict) else {}
    request_id = entry_object.get("id")
    request_key = request_id.strip() if isinstance(request_id, str) and request_id.strip() else f"manifest-index-{index}"
    return BatchRequest(
        index=index,
        request_key=request_key,
        item=item,
        source_url=entry_object.get("source_url") if isinstance(entry_object.get("source_url"), str) else None,
        pdf_file=None,
        pdf_url=None,
        pdf_source_url=None,
        pdf_title="PDF",
        referrer=None,
        notes=[],
        tags=[],
        arxiv_comment=None,
        reading_status=None,
        priority=None,
        pdf_sources=[],
        parse_error=error,
    )


def parse_manifest(
    path: Path,
    cli_collection: str | None,
    cli_target_id: str | None,
    *,
    require_pdf: bool = True,
) -> tuple[str, str | None, list[BatchRequest]]:
    payload = load_json(path)
    if isinstance(payload, list):
        manifest = {"papers": payload}
    elif isinstance(payload, dict):
        manifest = payload
    else:
        raise single.ImportFailure("invalid_manifest", "Batch manifest must be an object or an array")

    collection = cli_collection or manifest.get("collection")
    if not isinstance(collection, str) or not collection.strip():
        raise single.ImportFailure("invalid_manifest", "Provide --collection or a non-empty manifest 'collection'")
    target_id = cli_target_id or manifest.get("target_id")
    if target_id is not None and (not isinstance(target_id, str) or not target_id.strip()):
        raise single.ImportFailure("invalid_manifest", "Manifest 'target_id' must be a non-empty string")

    papers = manifest.get("papers")
    if not isinstance(papers, list) or not papers:
        raise single.ImportFailure("invalid_manifest", "Manifest requires a non-empty 'papers' array")
    default_tags = string_list(manifest.get("tags"), "tags")
    default_notes = string_list(manifest.get("notes"), "notes")
    # Keep an omitted status as None: append_notes_and_tags owns the to-read
    # default so an explicit #status/... tag on the item can take precedence.
    default_reading_status = single.canonical_workflow_value(
        optional_string(manifest.get("reading_status"), "reading_status"),
        "reading_status",
        single.READING_STATUS_OPTIONS,
    )
    default_priority = single.canonical_workflow_value(
        optional_string(manifest.get("priority"), "priority"),
        "priority",
        single.PRIORITIES,
    )
    manifest_dir = path.resolve().parent

    requests: list[BatchRequest] = []
    for index, entry in enumerate(papers):
        try:
            if not isinstance(entry, dict):
                raise single.ImportFailure("invalid_manifest", f"Paper {index} must be an object")
            unknown = sorted(set(entry) - PAPER_FIELDS)
            if unknown:
                raise single.ImportFailure(
                    "invalid_manifest",
                    f"Paper {index} contains unknown keys: {', '.join(unknown)}",
                )
            if "item" in entry and "item_json" in entry:
                raise single.ImportFailure("invalid_manifest", f"Paper {index} cannot contain both 'item' and 'item_json'")
            if "item" in entry:
                item = single.validate_item(entry["item"])
            elif "item_json" in entry:
                item_path = resolve_optional_path(entry["item_json"], manifest_dir, f"papers[{index}].item_json")
                item = single.load_item(item_path)
            else:
                raise single.ImportFailure("invalid_manifest", f"Paper {index} requires 'item' or 'item_json'")

            request_key = entry.get("id") or single.item_identity(item)
            if not isinstance(request_key, str) or not request_key.strip():
                raise single.ImportFailure("invalid_manifest", f"Paper {index} has an invalid request id")
            pdf_source_url = optional_string(entry.get("pdf_source_url"), f"papers[{index}].pdf_source_url")
            pdf_title = optional_string(entry.get("pdf_title"), f"papers[{index}].pdf_title", "PDF")
            referrer = optional_string(entry.get("referrer"), f"papers[{index}].referrer")
            pdf_sources = parse_pdf_sources(
                entry,
                index=index,
                manifest_dir=manifest_dir,
                require_pdf=require_pdf,
                default_pdf_source_url=pdf_source_url,
                default_pdf_title=pdf_title,
                default_referrer=referrer,
            )
            entry_tags = string_list(entry.get("tags"), f"papers[{index}].tags")
            entry_notes = string_list(entry.get("notes"), f"papers[{index}].notes")
            reading_status = single.canonical_workflow_value(
                optional_string(entry.get("reading_status"), f"papers[{index}].reading_status", default_reading_status),
                "reading_status",
                single.READING_STATUS_OPTIONS,
            )
            priority = single.canonical_workflow_value(
                optional_string(entry.get("priority"), f"papers[{index}].priority", default_priority),
                "priority",
                single.PRIORITIES,
            )
            requests.append(
                BatchRequest(
                    index=index,
                    request_key=request_key.strip(),
                    item=item,
                    source_url=optional_string(entry.get("source_url"), f"papers[{index}].source_url"),
                    pdf_file=pdf_sources[0].get("pdf_file") if len(pdf_sources) == 1 else None,
                    pdf_url=pdf_sources[0].get("pdf_url") if len(pdf_sources) == 1 else None,
                    pdf_source_url=pdf_source_url,
                    pdf_title=pdf_title,
                    referrer=referrer,
                    notes=default_notes + entry_notes,
                    tags=list(dict.fromkeys(default_tags + entry_tags)),
                    arxiv_comment=optional_string(entry.get("arxiv_comment"), f"papers[{index}].arxiv_comment"),
                    reading_status=reading_status,
                    priority=priority,
                    pdf_sources=pdf_sources,
                )
            )
        except single.ImportFailure as error:
            requests.append(invalid_batch_request(index, entry, error))
    return collection.strip(), target_id.strip() if isinstance(target_id, str) else None, requests


def default_ledger_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(manifest_path.stem + ".zotero-results.jsonl")


def read_ledger_records(ledger: Path) -> list[dict]:
    if not ledger.exists():
        return []
    records: list[dict] = []
    try:
        with ledger.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise single.ImportFailure(
                        "invalid_ledger",
                        f"Ledger {ledger} contains invalid JSON on line {line_number}",
                    ) from error
                if not isinstance(record, dict):
                    raise single.ImportFailure(
                        "invalid_ledger",
                        f"Ledger {ledger} line {line_number} must contain a JSON object",
                    )
                records.append(record)
    except OSError as error:
        raise single.ImportFailure("invalid_ledger", f"Cannot read ledger {ledger}: {error}") from error
    return records


def original_success_record(record: dict) -> dict | None:
    status = record.get("status")
    if status == "saved_with_pdf":
        return record
    if status != "skipped_completed":
        return None
    original = record.get("original_record")
    if isinstance(original, dict) and original.get("status") == "saved_with_pdf":
        return original
    if record.get("previous_status") == "saved_with_pdf":
        # Compatibility with ledgers written before original_record was added.
        restored = dict(record)
        restored["status"] = "saved_with_pdf"
        restored["recovered_from_legacy_resume_record"] = True
        return restored
    return None


def completed_from_records(records: list[dict], resume_scope: dict | None = None) -> dict[str, dict]:
    completed: dict[str, dict] = {}
    for record in records:
        if resume_scope is not None and record.get("resume_scope") != resume_scope:
            continue
        key = record.get("request_key")
        if not isinstance(key, str):
            continue
        original = original_success_record(record)
        if original is None:
            continue
        # A real saved result always supersedes a reconstructed legacy resume
        # record, while later skipped_completed entries never erase it.
        current = completed.get(key)
        if current is None or current.get("recovered_from_legacy_resume_record"):
            completed[key] = original
    return completed


def pending_review_from_records(
    records: list[dict],
    resume_scope: dict | None = None,
) -> dict[str, dict]:
    completed = completed_from_records(records, resume_scope)
    pending: dict[str, dict] = {}
    for record in records:
        if resume_scope is not None and record.get("resume_scope") != resume_scope:
            continue
        key = record.get("request_key")
        if not isinstance(key, str) or key in completed:
            continue
        if record.get("metadata_saved") is True:
            original = record.get("original_record")
            pending[key] = original if isinstance(original, dict) else record
    return pending


def read_completed(ledger: Path, resume_scope: dict | None = None) -> dict[str, dict]:
    """Return canonical successes without weakening them across repeated resumes."""

    return completed_from_records(read_ledger_records(ledger), resume_scope)
    return completed


def append_record(ledger: Path, record: dict) -> None:
    ledger.parent.mkdir(parents=True, exist_ok=True)
    try:
        with ledger.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as error:
        raise single.ImportFailure("ledger_write_failed", f"Cannot append to ledger {ledger}: {error}") from error


def write_summary_file(path: Path, summary: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def acquire_lock(ledger: Path) -> LedgerLock:
    lock_path = Path(str(ledger) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        handle.close()
        raise single.ImportFailure("batch_locked", f"Another batch appears to be using {ledger}") from error
    handle.seek(0)
    handle.truncate()
    handle.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    )
    handle.flush()
    return LedgerLock(handle=handle, path=lock_path)


def release_lock(lock: LedgerLock) -> None:
    try:
        lock.handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.handle.close()


def make_record(
    run_id: str,
    request: BatchRequest,
    status: str,
    *,
    resume_scope: dict | None = None,
    **details,
) -> LedgerRecord:
    record: LedgerRecord = {
        "run_id": run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index": request.index,
        "request_key": request.request_key,
        "title": request.item.get("title"),
        "source_url": request.source_url or request.item.get("url"),
        "status": status,
        **details,
    }
    if resume_scope is not None:
        record["resume_scope"] = dict(resume_scope)
    return record


def make_resume_record(
    run_id: str,
    request: BatchRequest,
    original: dict,
    resume_scope: dict,
) -> LedgerRecord:
    evidence_fields = (
        "item_key",
        "possible_duplicate_count",
        "possible_duplicate_keys",
        "in_target_collection",
        "pdf_count",
        "pdf_verified",
        "pdf_size",
        "pdf_sha256",
        "pdf_download_attempts",
        "pdf_source_index",
        "pdf_source_url",
        "pdf_source_attempts",
        "workflow_tags",
        "arxiv_comment",
    )
    evidence = {field: original[field] for field in evidence_fields if field in original}
    return make_record(
        run_id,
        request,
        "skipped_completed",
        resume_scope=resume_scope,
        previous_status=original.get("status"),
        original_status=original.get("status"),
        original_timestamp=original.get("timestamp"),
        original_run_id=original.get("run_id"),
        original_record=dict(original),
        **evidence,
    )


def make_review_hold_record(
    run_id: str,
    request: BatchRequest,
    original: dict,
    resume_scope: dict,
) -> LedgerRecord:
    return make_record(
        run_id,
        request,
        "skipped_needs_review",
        resume_scope=resume_scope,
        message="A prior write may have saved metadata; automatic retry is suppressed to avoid a duplicate",
        previous_status=original.get("status"),
        original_status=original.get("status"),
        original_timestamp=original.get("timestamp"),
        original_run_id=original.get("run_id"),
        original_record=dict(original),
        metadata_saved=True,
        item_key=original.get("item_key"),
    )


def canonical_task_record(record: LedgerRecord) -> LedgerRecord:
    if record.get("status") not in {"skipped_completed", "skipped_needs_review"}:
        return dict(record)
    if record.get("status") == "skipped_completed":
        original = original_success_record(record)
    else:
        candidate = record.get("original_record")
        original = candidate if isinstance(candidate, dict) else None
    if not isinstance(original, dict):
        return dict(record)
    task_record = dict(original)
    task_record.update(
        {
            "index": record.get("index"),
            "request_key": record.get("request_key"),
            "title": record.get("title"),
            "source_url": record.get("source_url"),
            "resumed_from_ledger": True,
            "latest_run_id": record.get("run_id"),
            "latest_timestamp": record.get("timestamp"),
        }
    )
    return task_record


def status_counts(records: list[LedgerRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = str(record.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts


def summarize(
    records: list[LedgerRecord],
    total: int,
    collection: str,
    ledger: Path,
    aborted: bool,
    prior_ledger_records: list[dict] | None = None,
    resume_scope: dict | None = None,
    safety_level: str = single.DEFAULT_SAFETY_LEVEL,
) -> BatchSummary:
    task_results = [canonical_task_record(record) for record in records]
    counts = status_counts(task_results)
    this_run_counts = status_counts(records)
    completed = sum(counts.get(status, 0) for status in SUCCESS_STATUSES)
    needs_action = sum(counts.get(status, 0) for status in NEEDS_ACTION_STATUSES)
    failed = total - completed - needs_action
    possible_duplicate_items = sum(
        1 for record in task_results if int(record.get("possible_duplicate_count", 0)) > 0
    )
    successful_keys = {
        record.get("request_key")
        for record in task_results
        if record.get("status") in SUCCESS_STATUSES
    }
    historical_failure_items = [
        {
            "request_key": record.get("request_key"),
            "status": record.get("status"),
            "timestamp": record.get("timestamp"),
            "failure_stage": record.get("failure_stage"),
            "message": record.get("message"),
        }
        for record in (prior_ledger_records or [])
        if record.get("request_key") in successful_keys
        and record.get("status") not in SUCCESS_STATUSES
        and record.get("status") not in {"not_attempted", "skipped_duplicate_in_manifest"}
    ]
    currently_unresolved = [
        {
            "index": record.get("index"),
            "request_key": record.get("request_key"),
            "status": record.get("status"),
            "message": record.get("message"),
        }
        for record in task_results
        if record.get("status") not in SUCCESS_STATUSES
    ]
    return {
        "status": "complete" if failed == 0 and needs_action == 0 and not aborted else "completed_with_issues",
        "collection": collection,
        "total": total,
        "completed": completed,
        "needs_action": needs_action,
        "failed_or_not_attempted": failed,
        "possible_duplicate_items": possible_duplicate_items,
        "aborted": aborted,
        "counts": counts,
        "this_run_counts": this_run_counts,
        "ledger": str(ledger.resolve()),
        "results": records,
        "task_results": task_results,
        "historical_transient_failures": len(historical_failure_items),
        "historical_failure_items": historical_failure_items,
        "currently_unresolved": currently_unresolved,
        "resume_scope": dict(resume_scope or {}),
        "safety_level": safety_level,
    }


def build_resume_scope(base_url: str, collection: str, target_id: str | None) -> dict:
    return {
        "version": 1,
        "base_url": base_url.rstrip("/"),
        "collection": single.normalized(collection),
        "target_id": target_id,
    }


def emit_progress(enabled: bool, event: str, **details) -> None:
    if not enabled:
        return
    print(
        json.dumps(
            {
                "event": event,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                **details,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        file=sys.stderr,
        flush=True,
    )


def run_batch(args: argparse.Namespace) -> BatchSummary:
    collection, target_id, requests = parse_manifest(
        args.manifest,
        args.collection,
        args.target_id,
        require_pdf=not args.dry_run,
    )
    ledger = args.ledger or default_ledger_path(args.manifest)
    resume_scope = build_resume_scope(args.base_url, collection, target_id)
    prior_ledger_records = read_ledger_records(ledger) if args.resume else []
    completed = completed_from_records(prior_ledger_records, resume_scope)
    pending_review = pending_review_from_records(prior_ledger_records, resume_scope)
    unique_requests: dict[str, BatchRequest] = {}
    for request in requests:
        if request.parse_error is None:
            unique_requests.setdefault(request.request_key, request)
    progress_enabled = getattr(args, "progress", True)
    context = None
    if any(key not in completed and key not in pending_review for key in unique_requests):
        emit_progress(progress_enabled, "batch_preflight_started", collection=collection)
        context = single.prepare_context(args.base_url, collection, target_id)
        emit_progress(
            progress_enabled,
            "batch_preflight_finished",
            collection=collection,
            resolved_target_id=context.target.get("id"),
            collection_key=context.collection_key,
        )
    elif unique_requests:
        emit_progress(
            progress_enabled,
            "batch_resume_from_ledger",
            collection=collection,
            completed=len(completed),
            needs_review=len(pending_review),
        )
    run_id = "batch-" + uuid.uuid4().hex
    records: list[LedgerRecord] = []
    seen: set[str] = set()
    aborted = False

    for position, request in enumerate(requests):
        paper_started = time.monotonic()
        emit_progress(
            progress_enabled,
            "paper_started",
            index=request.index,
            position=position + 1,
            total=len(requests),
            request_key=request.request_key,
            title=request.item.get("title"),
        )
        if request.parse_error is not None:
            record = make_record(
                run_id,
                request,
                "invalid_manifest_entry",
                resume_scope=resume_scope,
                message=request.parse_error.message,
                original_status=request.parse_error.status,
                failure_stage="manifest_validation",
                **request.parse_error.details,
            )
        elif request.request_key in seen:
            record = make_record(
                run_id,
                request,
                "skipped_duplicate_in_manifest",
                resume_scope=resume_scope,
                message="Duplicate request key in this manifest",
            )
        elif request.request_key in completed:
            previous = completed[request.request_key]
            record = make_resume_record(run_id, request, previous, resume_scope)
        elif request.request_key in pending_review:
            record = make_review_hold_record(
                run_id,
                request,
                pending_review[request.request_key],
                resume_scope,
            )
        else:
            try:
                def paper_progress(event: dict) -> None:
                    emit_progress(
                        progress_enabled,
                        str(event.get("event", "paper_progress")),
                        index=request.index,
                        position=position + 1,
                        total=len(requests),
                        request_key=request.request_key,
                        **{key: value for key, value in event.items() if key != "event"},
                    )

                result = single.import_item(
                    item=request.item,
                    collection=collection,
                    target_id=target_id,
                    pdf_sources=request.pdf_sources,
                    pdf_title=request.pdf_title,
                    source_url=request.source_url,
                    referrer=request.referrer,
                    notes=request.notes,
                    tags=request.tags,
                    arxiv_comment=request.arxiv_comment,
                    reading_status=request.reading_status,
                    priority=request.priority,
                    safety_level=getattr(args, "safety_level", single.DEFAULT_SAFETY_LEVEL),
                    connect_timeout=getattr(args, "connect_timeout", 15),
                    download_timeout=args.download_timeout,
                    download_attempts=getattr(args, "download_attempts", 3),
                    retry_backoff=getattr(args, "retry_backoff", 0.5),
                    per_paper_wall_timeout=getattr(args, "per_paper_wall_timeout", 300),
                    verify_timeout=args.verify_timeout,
                    dry_run=args.dry_run,
                    base_url=args.base_url,
                    context=context,
                    progress_callback=paper_progress,
                )
                status = result.pop("status")
                result.pop("title", None)
                record = make_record(run_id, request, status, resume_scope=resume_scope, **result)
            except single.ImportFailure as error:
                record = make_record(
                    run_id,
                    request,
                    error.status,
                    resume_scope=resume_scope,
                    message=error.message,
                    **error.details,
                )
            except Exception as error:
                record = make_record(
                    run_id,
                    request,
                    "internal_error",
                    resume_scope=resume_scope,
                    message=f"Unexpected {type(error).__name__}: {error}",
                    failure_stage="paper_import",
                    exception_type=type(error).__name__,
                )
        seen.add(request.request_key)
        record["elapsed_seconds"] = round(time.monotonic() - paper_started, 3)
        append_record(ledger, record)
        records.append(record)
        emit_progress(
            progress_enabled,
            "paper_finished",
            index=request.index,
            position=position + 1,
            total=len(requests),
            request_key=request.request_key,
            status=record["status"],
            elapsed_seconds=record["elapsed_seconds"],
        )

        if record["status"] in FATAL_STATUSES or (args.stop_on_error and record["status"] not in SUCCESS_STATUSES):
            aborted = True
            for remaining in requests[position + 1 :]:
                if remaining.request_key in seen:
                    skipped = make_record(
                        run_id,
                        remaining,
                        "skipped_duplicate_in_manifest",
                        resume_scope=resume_scope,
                        message="Duplicate request key in this manifest",
                    )
                elif remaining.request_key in completed:
                    skipped = make_resume_record(
                        run_id,
                        remaining,
                        completed[remaining.request_key],
                        resume_scope,
                    )
                elif remaining.request_key in pending_review:
                    skipped = make_review_hold_record(
                        run_id,
                        remaining,
                        pending_review[remaining.request_key],
                        resume_scope,
                    )
                else:
                    skipped = make_record(
                        run_id,
                        remaining,
                        "not_attempted",
                        resume_scope=resume_scope,
                        message=f"Batch stopped after {request.request_key}: {record['status']}",
                    )
                seen.add(remaining.request_key)
                append_record(ledger, skipped)
                records.append(skipped)
            break

    summary = summarize(
        records,
        len(requests),
        collection,
        ledger,
        aborted,
        prior_ledger_records=prior_ledger_records,
        resume_scope=resume_scope,
        safety_level=getattr(args, "safety_level", single.DEFAULT_SAFETY_LEVEL),
    )
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="UTF-8 JSON batch manifest")
    parser.add_argument("--collection", help="Exact target collection; overrides manifest collection")
    parser.add_argument("--target-id", help="Connector target ID; overrides manifest target_id")
    parser.add_argument("--ledger", type=Path, help="Append-only JSONL result ledger")
    parser.add_argument("--summary-json", type=Path, help="Optional path for the final JSON summary")
    parser.add_argument(
        "--safety-level",
        choices=sorted(single.SAFETY_LEVELS),
        default=single.DEFAULT_SAFETY_LEVEL,
        help="Target-checking policy; balanced confirms once before the first write",
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
    parser.add_argument("--dry-run", action="store_true", help="Check every paper and report possible duplicates without writing")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop after the first incomplete paper")
    parser.add_argument(
        "--no-progress",
        dest="progress",
        action="store_false",
        help="Suppress JSON progress events on stderr",
    )
    parser.set_defaults(progress=True)
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore prior successful ledger entries")
    parser.set_defaults(resume=True)
    parser.add_argument("--base-url", default=single.DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = args.ledger or default_ledger_path(args.manifest)
    lock = None
    summary: dict
    exit_code = 70
    try:
        lock = acquire_lock(ledger)
        summary = run_batch(args)
        exit_code = 0 if summary["status"] == "complete" else 3
    except single.ImportFailure as error:
        summary = {
            "status": error.status,
            "message": error.message,
            "aborted": True,
            "ledger": str(ledger.resolve()),
            **error.details,
        }
        exit_code = error.exit_code
    except Exception as error:
        summary = {
            "status": "internal_error",
            "message": f"Unexpected {type(error).__name__}: {error}",
            "failure_stage": "batch_import",
            "aborted": True,
            "ledger": str(ledger.resolve()),
        }
    finally:
        if lock is not None:
            try:
                release_lock(lock)
            except Exception as error:
                if "summary" not in locals():
                    summary = {
                        "status": "internal_error",
                        "message": f"Unexpected {type(error).__name__} while releasing the ledger lock: {error}",
                        "failure_stage": "lock_release",
                        "aborted": True,
                        "ledger": str(ledger.resolve()),
                    }
                else:
                    summary["lock_release_error"] = str(error)
                if exit_code == 0:
                    exit_code = 70
    if args.summary_json:
        try:
            write_summary_file(args.summary_json, summary)
        except OSError as error:
            summary["summary_write_error"] = str(error)
            if exit_code == 0:
                exit_code = 2
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    single.configure_utf8_stdio()
    sys.exit(main())
