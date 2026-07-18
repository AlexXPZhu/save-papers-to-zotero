#!/usr/bin/env python3
"""Import a manifest of papers sequentially through Zotero's local Connector server."""

from __future__ import annotations

import argparse
import json
import os
import sys
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
NEEDS_ACTION_STATUSES: set[str] = set()
FATAL_STATUSES = {
    "connection_error",
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
}


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


class BatchSummary(TypedDict):
    status: str
    collection: str
    total: int
    completed: int
    needs_action: int
    failed_or_not_attempted: int
    possible_duplicate_items: int
    aborted: bool
    counts: dict[str, int]
    ledger: str
    results: list[LedgerRecord]


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
        if not isinstance(entry, dict):
            raise single.ImportFailure("invalid_manifest", f"Paper {index} must be an object")
        if "item" in entry and "item_json" in entry:
            raise single.ImportFailure("invalid_manifest", f"Paper {index} cannot contain both 'item' and 'item_json'")
        if "item" in entry:
            item = single.validate_item(entry["item"])
        elif "item_json" in entry:
            item_path = resolve_optional_path(entry["item_json"], manifest_dir, f"papers[{index}].item_json")
            item = single.load_item(item_path)
        else:
            raise single.ImportFailure("invalid_manifest", f"Paper {index} requires 'item' or 'item_json'")

        pdf_file = resolve_optional_path(entry.get("pdf_file"), manifest_dir, f"papers[{index}].pdf_file")
        pdf_url = optional_string(entry.get("pdf_url"), f"papers[{index}].pdf_url")
        if require_pdf and bool(pdf_file) == bool(pdf_url):
            raise single.ImportFailure("invalid_manifest", f"Paper {index} requires exactly one of 'pdf_file' or 'pdf_url'")
        if not require_pdf and pdf_file and pdf_url:
            raise single.ImportFailure("invalid_manifest", f"Paper {index} cannot contain both 'pdf_file' and 'pdf_url'")

        request_key = entry.get("id") or single.item_identity(item)
        if not isinstance(request_key, str) or not request_key.strip():
            raise single.ImportFailure("invalid_manifest", f"Paper {index} has an invalid request id")
        entry_tags = string_list(entry.get("tags"), f"papers[{index}].tags")
        entry_notes = string_list(entry.get("notes"), f"papers[{index}].notes")
        reading_status = single.canonical_workflow_value(
            optional_string(
                entry.get("reading_status"),
                f"papers[{index}].reading_status",
                default_reading_status,
            ),
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
                pdf_file=pdf_file,
                pdf_url=pdf_url,
                pdf_source_url=optional_string(entry.get("pdf_source_url"), f"papers[{index}].pdf_source_url"),
                pdf_title=optional_string(entry.get("pdf_title"), f"papers[{index}].pdf_title", "PDF"),
                referrer=optional_string(entry.get("referrer"), f"papers[{index}].referrer"),
                notes=default_notes + entry_notes,
                tags=list(dict.fromkeys(default_tags + entry_tags)),
                arxiv_comment=optional_string(
                    entry.get("arxiv_comment"),
                    f"papers[{index}].arxiv_comment",
                ),
                reading_status=reading_status,
                priority=priority,
            )
        )
    return collection.strip(), target_id.strip() if isinstance(target_id, str) else None, requests


def default_ledger_path(manifest_path: Path) -> Path:
    return manifest_path.with_name(manifest_path.stem + ".zotero-results.jsonl")


def read_completed(ledger: Path) -> dict[str, dict]:
    if not ledger.exists():
        return {}
    completed: dict[str, dict] = {}
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
                key = record.get("request_key")
                if isinstance(key, str) and record.get("status") in RESUMABLE_SUCCESS_STATUSES:
                    completed[key] = record
    except OSError as error:
        raise single.ImportFailure("invalid_ledger", f"Cannot read ledger {ledger}: {error}") from error
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


def make_record(run_id: str, request: BatchRequest, status: str, **details) -> LedgerRecord:
    return {
        "run_id": run_id,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "index": request.index,
        "request_key": request.request_key,
        "title": request.item.get("title"),
        "source_url": request.source_url or request.item.get("url"),
        "status": status,
        **details,
    }


def summarize(
    records: list[LedgerRecord],
    total: int,
    collection: str,
    ledger: Path,
    aborted: bool,
) -> BatchSummary:
    counts: dict[str, int] = {}
    for record in records:
        counts[record["status"]] = counts.get(record["status"], 0) + 1
    completed = sum(counts.get(status, 0) for status in SUCCESS_STATUSES)
    needs_action = sum(counts.get(status, 0) for status in NEEDS_ACTION_STATUSES)
    failed = total - completed - needs_action
    possible_duplicate_items = sum(
        1 for record in records if int(record.get("possible_duplicate_count", 0)) > 0
    )
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
        "ledger": str(ledger.resolve()),
        "results": records,
    }


def run_batch(args: argparse.Namespace) -> BatchSummary:
    collection, target_id, requests = parse_manifest(
        args.manifest,
        args.collection,
        args.target_id,
        require_pdf=not args.dry_run,
    )
    ledger = args.ledger or default_ledger_path(args.manifest)
    completed = read_completed(ledger) if args.resume else {}
    context = single.prepare_context(args.base_url, collection, target_id)
    run_id = "batch-" + uuid.uuid4().hex
    records: list[LedgerRecord] = []
    seen: set[str] = set()
    aborted = False

    for position, request in enumerate(requests):
        if request.request_key in seen:
            record = make_record(
                run_id,
                request,
                "skipped_duplicate_in_manifest",
                message="Duplicate request key in this manifest",
            )
            append_record(ledger, record)
            records.append(record)
            continue
        seen.add(request.request_key)

        if request.request_key in completed:
            previous = completed[request.request_key]
            record = make_record(
                run_id,
                request,
                "skipped_completed",
                previous_status=previous.get("status"),
                item_key=previous.get("item_key"),
            )
            append_record(ledger, record)
            records.append(record)
            continue

        try:
            result = single.import_item(
                item=request.item,
                collection=collection,
                target_id=target_id,
                pdf_file=request.pdf_file,
                pdf_url=request.pdf_url,
                pdf_source_url=request.pdf_source_url,
                pdf_title=request.pdf_title,
                source_url=request.source_url,
                referrer=request.referrer,
                notes=request.notes,
                tags=request.tags,
                arxiv_comment=request.arxiv_comment,
                reading_status=request.reading_status,
                priority=request.priority,
                download_timeout=args.download_timeout,
                verify_timeout=args.verify_timeout,
                dry_run=args.dry_run,
                base_url=args.base_url,
                context=context,
            )
            status = result.pop("status")
            result.pop("title", None)
            record = make_record(run_id, request, status, **result)
        except single.ImportFailure as error:
            record = make_record(run_id, request, error.status, message=error.message, **error.details)
        append_record(ledger, record)
        records.append(record)

        if record["status"] in FATAL_STATUSES or (args.stop_on_error and record["status"] not in SUCCESS_STATUSES):
            aborted = True
            for remaining in requests[position + 1 :]:
                skipped = make_record(
                    run_id,
                    remaining,
                    "not_attempted",
                    message=f"Batch stopped after {request.request_key}: {record['status']}",
                )
                append_record(ledger, skipped)
                records.append(skipped)
            break

    summary = summarize(records, len(requests), collection, ledger, aborted)
    if args.summary_json:
        args.summary_json.parent.mkdir(parents=True, exist_ok=True)
        args.summary_json.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True, help="UTF-8 JSON batch manifest")
    parser.add_argument("--collection", help="Exact target collection; overrides manifest collection")
    parser.add_argument("--target-id", help="Connector target ID; overrides manifest target_id")
    parser.add_argument("--ledger", type=Path, help="Append-only JSONL result ledger")
    parser.add_argument("--summary-json", type=Path, help="Optional path for the final JSON summary")
    parser.add_argument("--download-timeout", type=float, default=60)
    parser.add_argument("--verify-timeout", type=float, default=30)
    parser.add_argument("--dry-run", action="store_true", help="Check every paper and report possible duplicates without writing")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop after the first incomplete paper")
    parser.add_argument("--no-resume", dest="resume", action="store_false", help="Ignore prior successful ledger entries")
    parser.set_defaults(resume=True)
    parser.add_argument("--base-url", default=single.DEFAULT_BASE_URL, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ledger = args.ledger or default_ledger_path(args.manifest)
    lock = None
    try:
        lock = acquire_lock(ledger)
        summary = run_batch(args)
    except single.ImportFailure as error:
        summary = {"status": error.status, "message": error.message, **error.details}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return error.exit_code
    finally:
        if lock is not None:
            release_lock(lock)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["status"] == "complete" else 3


if __name__ == "__main__":
    sys.exit(main())
