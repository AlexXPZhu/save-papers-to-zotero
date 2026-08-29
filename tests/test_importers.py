from __future__ import annotations

import argparse
import contextlib
import http.client
import io
import json
import socket
import ssl
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock


TEST_PARENT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = TEST_PARENT / "save-papers-to-zotero" / "scripts"
if not SCRIPT_DIR.is_dir():
    SCRIPT_DIR = TEST_PARENT / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import zotero_batch_import as batch  # noqa: E402
import zotero_connector_import as single  # noqa: E402


class FakeZotero:
    def __init__(self, root: Path):
        self.root = root
        self.parents: list[dict] = []
        self.children: dict[str, list[dict]] = {}
        self.sessions: dict[str, dict] = {}
        self.update_tags: dict[str, list[str]] = {}
        self.next_parent = 1
        self.next_attachment = 1
        self.next_note = 1
        self.note_children_queries = 0
        self.targets = [{"id": "C1", "name": "Route A", "filesEditable": True}]
        self.collections = [{"key": "COLL1", "data": {"key": "COLL1", "name": "Route A"}}]
        self.rename_target_after_attachments: int | None = None
        self.rename_target_on_pdf_download = False
        self.saved_attachment_count = 0
        self.ping_count = 0
        self.target_lookup_count = 0
        self.collection_lookup_count = 0
        self.corrupt_attachments = False
        self.drip_hits = 0

    def add_existing(self, item: dict, *, with_pdf: bool = True, in_collection: bool = True) -> str:
        key = f"EXIST{self.next_parent:03d}"
        self.next_parent += 1
        stored = dict(item)
        stored.update({"key": key, "collections": ["COLL1"] if in_collection else []})
        self.parents.append(stored)
        self.children[key] = []
        if with_pdf:
            self._add_pdf(key, "Existing PDF")
        return key

    def _add_pdf(self, parent_key: str, title: str, content: bytes | None = None) -> str:
        key = f"PDF{self.next_attachment:05d}"
        self.next_attachment += 1
        path = self.root / f"{key}.pdf"
        path.write_bytes(content if content is not None else b"%PDF-1.4\n%%EOF\n")
        self.children.setdefault(parent_key, []).append(
            {
                "key": key,
                "data": {
                    "key": key,
                    "itemType": "attachment",
                    "parentItem": parent_key,
                    "title": title,
                    "contentType": "application/pdf",
                },
                "file_uri": path.resolve().as_uri(),
            }
        )
        return key

    def _add_note(self, parent_key: str, note: str) -> str:
        key = f"NOTE{self.next_note:04d}"
        self.next_note += 1
        self.children.setdefault(parent_key, []).append(
            {
                "key": key,
                "data": {
                    "key": key,
                    "itemType": "note",
                    "parentItem": parent_key,
                    "note": note,
                },
            }
        )
        return key

    def handler(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def send_json(self, status: int, payload: object, **headers):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for name, value in headers.items():
                    self.send_header(name.replace("_", "-"), value)
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                parsed = urllib.parse.urlsplit(self.path)
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                if parsed.path == "/connector/ping":
                    state.ping_count += 1
                    self.send_json(200, {}, X_Zotero_Version="9.0-test")
                    return
                if parsed.path == "/connector/getSelectedCollection":
                    state.target_lookup_count += 1
                    self.send_json(200, {"targets": state.targets})
                    return
                if parsed.path == "/connector/saveItems":
                    payload = json.loads(body)
                    item = dict(payload["items"][0])
                    key = f"ITEM{state.next_parent:04d}"
                    state.next_parent += 1
                    item.update({"key": key, "collections": []})
                    state.parents.append(item)
                    state.children[key] = []
                    for note in item.get("notes", []):
                        if isinstance(note, dict) and isinstance(note.get("note"), str):
                            state._add_note(key, note["note"])
                    state.sessions[payload["sessionID"]] = {
                        "key": key,
                        "connector_item_id": item["id"],
                    }
                    self.send_json(201, {})
                    return
                if parsed.path == "/connector/updateSession":
                    payload = json.loads(body)
                    target = next((target for target in state.targets if target["id"] == payload["target"]), None)
                    if not target or not target.get("filesEditable"):
                        self.send_json(409, {"error": "target unavailable"})
                        return
                    session = state.sessions[payload["sessionID"]]
                    parent = next(item for item in state.parents if item["key"] == session["key"])
                    parent["collections"] = ["COLL1"]
                    state.update_tags[payload["sessionID"]] = payload.get("tags", [])
                    self.send_json(200, {})
                    return
                if parsed.path == "/connector/saveAttachment":
                    metadata = json.loads(self.headers["X-Metadata"])
                    session = state.sessions[metadata["sessionID"]]
                    if metadata["parentItemID"] != session["connector_item_id"]:
                        self.send_json(400, {"error": "parent mismatch"})
                        return
                    stored_body = b"%PDF-1.4\nbroken\n%%EOF\n" if state.corrupt_attachments else body
                    state._add_pdf(session["key"], metadata["title"], stored_body)
                    state.saved_attachment_count += 1
                    if state.rename_target_after_attachments == state.saved_attachment_count:
                        state.targets[0]["name"] = "Route A renamed"
                        if state.collections:
                            state.collections[0]["data"]["name"] = "Route A renamed"
                    self.send_json(201, {})
                    return
                self.send_json(404, {"error": "not found"})

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                if parsed.path == "/reset.pdf":
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                if parsed.path == "/download.pdf":
                    if state.rename_target_on_pdf_download:
                        state.targets[0]["name"] = "Route A renamed"
                        if state.collections:
                            state.collections[0]["data"]["name"] = "Route A renamed"
                    body = b"%PDF-1.4\n%%EOF\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if parsed.path == "/drip.pdf":
                    state.drip_hits += 1
                    body = b"%PDF-1.4\n" + b"x" * 200 + b"\n%%EOF\n"
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    try:
                        for byte in body:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                            time.sleep(0.02)
                    except OSError:
                        pass
                    return
                if parsed.path == "/api/users/0/collections":
                    state.collection_lookup_count += 1
                    self.send_json(200, state.collections)
                    return
                if parsed.path == "/api/users/0/items":
                    query = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].casefold()
                    matches = []
                    for item in state.parents:
                        haystack = " ".join(
                            str(item.get(field, ""))
                            for field in ("title", "DOI", "archiveID", "extra", "url")
                        ).casefold()
                        if query in haystack:
                            matches.append({"key": item["key"], "data": dict(item)})
                    start = int(urllib.parse.parse_qs(parsed.query).get("start", ["0"])[0])
                    limit = int(urllib.parse.parse_qs(parsed.query).get("limit", ["100"])[0])
                    self.send_json(200, matches[start : start + limit])
                    return
                parts = parsed.path.strip("/").split("/")
                if (
                    len(parts) == 8
                    and parts[:4] == ["api", "users", "0", "items"]
                    and parts[5:] == ["file", "view", "url"]
                ):
                    attachment_key = parts[4]
                    for children in state.children.values():
                        for child in children:
                            if child["key"] == attachment_key:
                                body = child["file_uri"].encode("utf-8")
                                self.send_response(200)
                                self.send_header("Content-Length", str(len(body)))
                                self.end_headers()
                                self.wfile.write(body)
                                return
                    self.send_json(404, {"error": "missing attachment"})
                    return
                if len(parts) == 6 and parts[:4] == ["api", "users", "0", "items"] and parts[5] == "children":
                    parent_key = parts[4]
                    item_type = urllib.parse.parse_qs(parsed.query).get("itemType", [None])[0]
                    if item_type == "note":
                        state.note_children_queries += 1
                    public_children = [
                        {"key": child["key"], "data": child["data"]}
                        for child in state.children.get(parent_key, [])
                        if item_type is None or child["data"].get("itemType") == item_type
                    ]
                    self.send_json(200, public_children)
                    return
                self.send_json(404, {"error": "not found"})

        return Handler


class FakeZoteroServer:
    def __init__(self, root: Path):
        self.state = FakeZotero(root)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.state.handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def paper(number: int) -> dict:
    return {
        "itemType": "journalArticle",
        "title": f"Paper {number}",
        "creators": [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
        "DOI": f"10.1000/paper-{number}",
        "url": f"https://example.test/paper/{number}",
    }


class FakeDownloadResponse:
    def __init__(self, payload: bytes, *, declared_length: int | None = None):
        self.payload = payload
        self.offset = 0
        self.status = 200
        self.headers = {"Content-Length": str(declared_length if declared_length is not None else len(payload))}
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        if self.offset >= len(self.payload):
            return b""
        end = len(self.payload) if size < 0 else min(len(self.payload), self.offset + size)
        chunk = self.payload[self.offset : end]
        self.offset = end
        return chunk


class GeneratedDownloadResponse:
    def __init__(self, total_size: int):
        self.total_size = total_size
        self.offset = 0
        self.status = 200
        self.headers = {"Content-Length": str(total_size)}
        self.max_requested = 0
        self.prefix = b"%PDF-1.4\n"
        self.suffix = b"\n%%EOF\n"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1) -> bytes:
        self.max_requested = max(self.max_requested, size)
        if self.offset >= self.total_size:
            return b""
        end = self.total_size if size < 0 else min(self.total_size, self.offset + size)
        chunk = bytearray(b"x" * (end - self.offset))
        for marker, marker_start in (
            (self.prefix, 0),
            (self.suffix, self.total_size - len(self.suffix)),
        ):
            overlap_start = max(self.offset, marker_start)
            overlap_end = min(end, marker_start + len(marker))
            if overlap_start < overlap_end:
                chunk[overlap_start - self.offset : overlap_end - self.offset] = marker[
                    overlap_start - marker_start : overlap_end - marker_start
                ]
        self.offset = end
        return bytes(chunk)


class SingleImporterTests(unittest.TestCase):
    def test_identity_prefers_doi_then_arxiv_then_title(self):
        cases = [
            ({"title": "X", "DOI": "https://doi.org/10.1/ABC"}, "doi:10.1/abc"),
            ({"title": "X", "url": "https://arxiv.org/abs/2403.00476v2"}, "arxiv:2403.00476"),
            ({"title": "  Mixed   CASE "}, "title:mixed case"),
        ]
        for item, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(single.item_identity(item), expected)

    def test_import_verifies_new_pdf_and_allows_library_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            with FakeZoteroServer(root) as fake:
                requested = paper(1)
                requested["tags"] = [
                    {"tag": "Computer Science - Artificial Intelligence", "type": 1},
                    {"tag": "#status/to-read"},
                    {"tag": "#priority/low"},
                ]
                result = single.import_item(
                    item=requested,
                    collection="Route A",
                    pdf_file=pdf,
                    notes=["<p>AI note</p>"],
                    tags=["batch", "route-a", "#status/to-read"],
                    arxiv_comment="Comment: AAAI 2018. Code available at https://example.test/code",
                    reading_status="reading",
                    priority="high",
                    base_url=fake.base_url,
                    verify_timeout=2,
                )
                self.assertEqual(result["status"], "saved_with_pdf")
                self.assertTrue(result["pdf_verified"])
                self.assertEqual(fake.state.note_children_queries, 0)
                self.assertEqual(result["possible_duplicate_count"], 0)
                self.assertEqual(
                    result["arxiv_comment"],
                    "Comment: AAAI 2018. Code available at https://example.test/code",
                )
                self.assertEqual(result["workflow_tags"], ["#status/reading", "#priority/high"])
                self.assertEqual(len(fake.state.parents), 1)
                self.assertEqual(
                    fake.state.parents[0]["notes"],
                    [
                        {"note": "Comment: AAAI 2018. Code available at https://example.test/code"},
                        {"note": "<p>AI note</p>"},
                    ],
                )
                self.assertEqual(
                    fake.state.parents[0]["tags"],
                    [
                        {"tag": "Computer Science - Artificial Intelligence", "type": 1},
                        {"tag": "batch"},
                        {"tag": "route-a"},
                        {"tag": "#status/reading"},
                        {"tag": "#priority/high"},
                    ],
                )
                self.assertIn(
                    ["batch", "route-a", "#status/reading", "#priority/high"],
                    fake.state.update_tags.values(),
                )

                duplicate = single.import_item(
                    item=paper(1),
                    collection="Route A",
                    pdf_file=pdf,
                    base_url=fake.base_url,
                    verify_timeout=2,
                )
                self.assertEqual(duplicate["status"], "saved_with_pdf")
                self.assertEqual(duplicate["possible_duplicate_count"], 1)
                self.assertEqual(duplicate["possible_duplicate_keys"], [result["item_key"]])
                self.assertNotEqual(duplicate["item_key"], result["item_key"])
                self.assertTrue(duplicate["pdf_verified"])
                self.assertEqual(len(fake.state.parents), 2)

    def test_invalid_workflow_values_are_rejected(self):
        cases = [
            ({"reading_status": "done"}, "invalid_reading_status"),
            ({"priority": "urgent"}, "invalid_priority"),
            ({"arxiv_comment": "Comment:   "}, "invalid_arxiv_comment"),
        ]
        for values, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                with self.assertRaises(single.ImportFailure) as raised:
                    single.append_notes_and_tags(paper(1), [], [], **values)
                self.assertEqual(raised.exception.status, expected_status)

    def test_arxiv_comment_prefix_and_whitespace_are_normalized(self):
        cases = [
            ("Comment: some text", "Comment: some text"),
            ("  Comment:  Foo  ", "Comment: Foo"),
            ("comment: foo", "Comment: foo"),
            ("COMMENT:foo", "Comment: foo"),
            ("Line one\n  line two", "Comment: Line one line two"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(single.format_arxiv_comment(value), expected)

    def test_reading_status_defaults_to_to_read_and_can_be_overridden(self):
        default_item, default_session_tags = single.append_notes_and_tags(paper(1), [], [])
        self.assertIn({"tag": "#status/to-read"}, default_item["tags"])
        self.assertEqual(default_session_tags, ["#status/to-read"])

        reading_item, reading_session_tags = single.append_notes_and_tags(
            paper(1),
            [],
            [],
            reading_status="reading",
        )
        self.assertIn({"tag": "#status/reading"}, reading_item["tags"])
        self.assertEqual(reading_session_tags, ["#status/reading"])

        opt_out_item, opt_out_session_tags = single.append_notes_and_tags(
            paper(1),
            [],
            [],
            reading_status="none",
        )
        self.assertNotIn("tags", opt_out_item)
        self.assertEqual(opt_out_session_tags, [])

        tagged = paper(1)
        tagged["tags"] = [{"tag": "#status/reading"}]
        preserved_item, preserved_session_tags = single.append_notes_and_tags(tagged, [], [])
        self.assertEqual(preserved_item["tags"], [{"tag": "#status/reading"}])
        self.assertEqual(preserved_session_tags, [])

    def test_doi_match_is_reported_but_does_not_block_import(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            with FakeZoteroServer(root) as fake:
                existing = paper(2)
                existing["title"] = "Publisher title"
                existing_key = fake.state.add_existing(existing)
                requested = paper(2)
                requested["title"] = "Preprint title"
                result = single.import_item(
                    item=requested,
                    collection="Route A",
                    pdf_file=pdf,
                    base_url=fake.base_url,
                    verify_timeout=2,
                )
                self.assertEqual(result["status"], "saved_with_pdf")
                self.assertEqual(result["possible_duplicate_keys"], [existing_key])
                self.assertNotEqual(result["item_key"], existing_key)
                self.assertEqual(len(fake.state.parents), 2)

    def test_target_id_resolves_ambiguous_connector_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            with FakeZoteroServer(Path(temp)) as fake:
                fake.state.targets.append({"id": "C2", "name": "Route A", "filesEditable": True})
                with self.assertRaises(single.ImportFailure) as raised:
                    single.prepare_context(fake.base_url, "Route A")
                self.assertEqual(raised.exception.status, "target_ambiguous")

                context = single.prepare_context(fake.base_url, "Route A", "C1")
                self.assertEqual(context.target["id"], "C1")

    def test_collection_verification_unavailable_is_explicit(self):
        with tempfile.TemporaryDirectory() as temp:
            with FakeZoteroServer(Path(temp)) as fake:
                fake.state.collections = []
                with self.assertRaises(single.ImportFailure) as raised:
                    single.prepare_context(fake.base_url, "Route A")
                self.assertEqual(raised.exception.status, "collection_verification_unavailable")

    def test_missing_collection_message_guides_manual_creation(self):
        with tempfile.TemporaryDirectory() as temp:
            with FakeZoteroServer(Path(temp)) as fake:
                with self.assertRaises(single.ImportFailure) as raised:
                    single.prepare_context(fake.base_url, "Does Not Exist")
                self.assertEqual(raised.exception.status, "target_not_found")
                self.assertIn("create this collection manually", raised.exception.message)
                self.assertIn("cannot create collections", raised.exception.message)

    def test_target_is_revalidated_after_download_before_write(self):
        with tempfile.TemporaryDirectory() as temp:
            with FakeZoteroServer(Path(temp)) as fake:
                fake.state.rename_target_on_pdf_download = True
                with self.assertRaises(single.ImportFailure) as raised:
                    single.import_item(
                        item=paper(1),
                        collection="Route A",
                        pdf_url=fake.base_url + "/download.pdf",
                        base_url=fake.base_url,
                        verify_timeout=2,
                    )
                self.assertEqual(raised.exception.status, "target_not_found")
                self.assertEqual(fake.state.parents, [])

    def test_download_retries_common_transport_failures(self):
        payload = b"%PDF-1.4\n" + b"x" * 64 + b"\n%%EOF\n"
        cases = [
            (TimeoutError("read timed out"), "timeout"),
            (http.client.IncompleteRead(b"partial", 10), "incomplete"),
            (ssl.SSLEOFError(8, "TLS EOF"), "tls_eof"),
            (ConnectionResetError("reset"), "connection_reset"),
        ]
        for failure, label in cases:
            response = FakeDownloadResponse(payload)
            with self.subTest(label=label), mock.patch.object(
                single.urllib.request,
                "urlopen",
                side_effect=[failure, response],
            ):
                prepared = single.download_pdf_to_temp(
                    "https://example.test/paper.pdf",
                    referrer=None,
                    connect_timeout=1,
                    read_timeout=1,
                    max_attempts=2,
                    retry_backoff=0,
                    wall_timeout=5,
                )
                try:
                    self.assertEqual(prepared.download_attempts, 2)
                    self.assertEqual(prepared.size, len(payload))
                    self.assertTrue(prepared.path.exists())
                finally:
                    prepared.cleanup()

    def test_download_transport_failures_have_stage_specific_statuses(self):
        cases = [
            (TimeoutError("read timed out"), "pdf_download_timeout"),
            (http.client.IncompleteRead(b"partial", 10), "pdf_download_incomplete"),
            (ssl.SSLEOFError(8, "TLS EOF"), "pdf_download_tls_error"),
            (ConnectionResetError("reset"), "pdf_source_connection_error"),
        ]
        for failure, expected_status in cases:
            with self.subTest(expected_status=expected_status), mock.patch.object(
                single.urllib.request,
                "urlopen",
                side_effect=failure,
            ):
                with self.assertRaises(single.ImportFailure) as raised:
                    single.download_pdf_to_temp(
                        "https://example.test/paper.pdf",
                        referrer=None,
                        connect_timeout=1,
                        read_timeout=1,
                        max_attempts=1,
                        retry_backoff=0,
                        wall_timeout=5,
                    )
                self.assertEqual(raised.exception.status, expected_status)
                self.assertEqual(raised.exception.details["failure_stage"], "pdf_download")
                self.assertEqual(raised.exception.details["attempts"], 1)

    def test_incomplete_download_exhaustion_has_structured_status(self):
        payload = b"%PDF-1.4\npartial"
        responses = [FakeDownloadResponse(payload, declared_length=len(payload) + 20) for _ in range(3)]
        with mock.patch.object(single.urllib.request, "urlopen", side_effect=responses):
            with self.assertRaises(single.ImportFailure) as raised:
                single.download_pdf_to_temp(
                    "https://example.test/incomplete.pdf",
                    referrer=None,
                    connect_timeout=1,
                    read_timeout=1,
                    max_attempts=3,
                    retry_backoff=0,
                    wall_timeout=5,
                )
        self.assertEqual(raised.exception.status, "pdf_download_incomplete")
        self.assertEqual(raised.exception.details["attempts"], 3)
        self.assertEqual(raised.exception.details["failure_stage"], "pdf_download")
        self.assertEqual(raised.exception.details["source_url"], "https://example.test/incomplete.pdf")

    def test_large_download_is_chunked_to_a_temporary_file(self):
        payload = b"%PDF-" + b"x" * (single.DOWNLOAD_CHUNK_SIZE * 2 + 123) + b"\n%%EOF\n"
        response = FakeDownloadResponse(payload)
        with mock.patch.object(single.urllib.request, "urlopen", return_value=response):
            prepared = single.download_pdf_to_temp(
                "https://example.test/large.pdf",
                referrer=None,
                connect_timeout=1,
                read_timeout=1,
                max_attempts=1,
                retry_backoff=0,
                wall_timeout=5,
            )
        try:
            self.assertEqual(prepared.size, len(payload))
            self.assertGreaterEqual(len(response.read_sizes), 4)
            self.assertTrue(all(size == single.DOWNLOAD_CHUNK_SIZE for size in response.read_sizes))
        finally:
            prepared.cleanup()

    def test_hundred_megabyte_download_has_bounded_python_memory(self):
        total_size = 100 * 1024 * 1024
        response = GeneratedDownloadResponse(total_size)
        tracemalloc.start()
        try:
            with mock.patch.object(single.urllib.request, "urlopen", return_value=response):
                prepared = single.download_pdf_to_temp(
                    "https://example.test/100mb.pdf",
                    referrer=None,
                    connect_timeout=1,
                    read_timeout=1,
                    max_attempts=1,
                    retry_backoff=0,
                    wall_timeout=30,
                )
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        try:
            self.assertEqual(prepared.size, total_size)
            self.assertEqual(response.max_requested, single.DOWNLOAD_CHUNK_SIZE)
            self.assertLess(peak, 12 * 1024 * 1024)
        finally:
            prepared.cleanup()

    def test_truncated_pdf_without_eof_is_rejected(self):
        response = FakeDownloadResponse(b"%PDF-")
        with mock.patch.object(single.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(single.ImportFailure) as raised:
                single.download_pdf_to_temp(
                    "https://example.test/truncated.pdf",
                    referrer=None,
                    connect_timeout=1,
                    read_timeout=1,
                    max_attempts=1,
                    retry_backoff=0,
                    wall_timeout=5,
                )
        self.assertEqual(raised.exception.status, "invalid_pdf")
        self.assertIn("EOF", raised.exception.message)

    def test_zotero_read_only_operations_retry_transient_failures(self):
        transient = single.ImportFailure(
            "zotero_connection_error",
            "temporary local failure",
            failure_stage="zotero_request",
        )
        with mock.patch.object(
            single,
            "connector_post",
            side_effect=[transient, transient, (200, b"{}", {})],
        ) as connector, mock.patch.object(single.time, "sleep"):
            status, payload, _ = single.connector_read_post("http://127.0.0.1", "/connector/ping", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload, b"{}")
        self.assertEqual(connector.call_count, 3)

    def test_per_paper_wall_timeout_stops_a_drip_response_and_reports_real_attempts(self):
        with tempfile.TemporaryDirectory() as temp:
            with FakeZoteroServer(Path(temp)) as fake:
                started = time.monotonic()
                with self.assertRaises(single.ImportFailure) as raised:
                    single.download_pdf_to_temp(
                        fake.base_url + "/drip.pdf",
                        referrer=None,
                        connect_timeout=1,
                        read_timeout=1,
                        max_attempts=3,
                        retry_backoff=0,
                        wall_timeout=0.2,
                    )
                elapsed = time.monotonic() - started
                self.assertEqual(raised.exception.status, "pdf_download_timeout")
                self.assertEqual(raised.exception.details["attempts"], 1)
                self.assertEqual(fake.state.drip_hits, 1)
                self.assertLess(elapsed, 0.8)

    def test_post_write_verification_checks_stored_pdf_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\noriginal content\n%%EOF\n")
            with FakeZoteroServer(root) as fake:
                fake.state.corrupt_attachments = True
                with self.assertRaises(single.ImportFailure) as raised:
                    single.import_item(
                        item=paper(1),
                        collection="Route A",
                        pdf_file=pdf,
                        base_url=fake.base_url,
                        verify_timeout=0.05,
                    )
                self.assertEqual(raised.exception.status, "verification_failed")
                self.assertTrue(raised.exception.details["metadata_saved"])
                observation = raised.exception.details["last_observation"]
                self.assertFalse(observation["pdf_verified"])
                self.assertFalse(observation["pdf_files"][0]["content_matches"])


class BatchImporterTests(unittest.TestCase):
    def make_args(self, manifest: Path, ledger: Path, base_url: str) -> argparse.Namespace:
        return argparse.Namespace(
            manifest=manifest,
            collection=None,
            target_id=None,
            ledger=ledger,
            summary_json=None,
            safety_level="balanced",
            connect_timeout=2,
            download_timeout=2,
            download_attempts=3,
            retry_backoff=0,
            per_paper_wall_timeout=10,
            verify_timeout=2,
            dry_run=False,
            stop_on_error=False,
            progress=False,
            resume=True,
            base_url=base_url,
        )

    def test_omitted_manifest_status_remains_unset_for_explicit_item_tag(self):
        with tempfile.TemporaryDirectory() as temp:
            manifest = Path(temp) / "papers.json"
            tagged = paper(1)
            tagged["tags"] = [{"tag": "#status/reading"}]
            manifest.write_text(
                json.dumps({"collection": "Route A", "papers": [{"item": tagged}]}),
                encoding="utf-8",
            )

            _, _, requests = batch.parse_manifest(
                manifest,
                cli_collection=None,
                cli_target_id=None,
                require_pdf=False,
            )
            self.assertIsNone(requests[0].reading_status)
            enriched, session_tags = single.append_notes_and_tags(
                requests[0].item,
                requests[0].notes,
                requests[0].tags,
                reading_status=requests[0].reading_status,
            )
            self.assertEqual(enriched["tags"], [{"tag": "#status/reading"}])
            self.assertEqual(session_tags, [])

    def test_batch_continues_after_bad_pdf_and_resumes_successes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good1 = root / "good1.pdf"
            bad = root / "bad.pdf"
            good3 = root / "good3.pdf"
            good1.write_bytes(b"%PDF-1.4\n%%EOF\n")
            bad.write_text("not a pdf", encoding="utf-8")
            good3.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "collection": "Route A",
                        "tags": ["research"],
                        "papers": [
                            {"item": paper(1), "pdf_file": str(good1)},
                            {"item": paper(2), "pdf_file": str(bad)},
                            {"item": paper(3), "pdf_file": str(good3)},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                args = self.make_args(manifest, ledger, fake.base_url)
                first = batch.run_batch(args)
                self.assertEqual(first["completed"], 2)
                self.assertEqual(first["failed_or_not_attempted"], 1)
                for actual, expected in zip(
                    [record["status"] for record in first["results"]],
                    ["saved_with_pdf", "invalid_pdf", "saved_with_pdf"],
                    strict=True,
                ):
                    with self.subTest(run="first", expected=expected):
                        self.assertEqual(actual, expected)
                self.assertEqual(len(fake.state.parents), 2)

                second = batch.run_batch(args)
                for actual, expected in zip(
                    [record["status"] for record in second["results"]],
                    ["skipped_completed", "invalid_pdf", "skipped_completed"],
                    strict=True,
                ):
                    with self.subTest(run="resume", expected=expected):
                        self.assertEqual(actual, expected)
                self.assertEqual(len(fake.state.parents), 2)

    def test_duplicate_manifest_request_is_skipped_without_second_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "collection": "Route A",
                        "papers": [
                            {"item": paper(1), "pdf_file": str(pdf)},
                            {"item": paper(1), "pdf_file": str(pdf)},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                summary = batch.run_batch(self.make_args(manifest, ledger, fake.base_url))
                self.assertEqual(summary["status"], "complete")
                self.assertEqual(summary["counts"]["skipped_duplicate_in_manifest"], 1)
                self.assertEqual(len(fake.state.parents), 1)
                self.assertIn({"tag": "#status/to-read"}, fake.state.parents[0]["tags"])

    def test_library_duplicate_is_saved_and_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "collection": "Route A",
                        "reading_status": "to-read",
                        "priority": "medium",
                        "papers": [
                            {
                                "item": paper(1),
                                "pdf_file": str(pdf),
                                "arxiv_comment": "Accepted at ExampleConf 2026",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                existing_key = fake.state.add_existing(paper(1))
                summary = batch.run_batch(self.make_args(manifest, ledger, fake.base_url))
                self.assertEqual(summary["status"], "complete")
                self.assertEqual(summary["possible_duplicate_items"], 1)
                result = summary["results"][0]
                self.assertEqual(result["status"], "saved_with_pdf")
                self.assertEqual(result["possible_duplicate_keys"], [existing_key])
                self.assertEqual(result["arxiv_comment"], "Comment: Accepted at ExampleConf 2026")
                self.assertEqual(result["workflow_tags"], ["#status/to-read", "#priority/medium"])
                self.assertNotEqual(result["item_key"], existing_key)
                self.assertEqual(len(fake.state.parents), 2)
                imported = fake.state.parents[-1]
                self.assertIn({"tag": "#status/to-read"}, imported["tags"])
                self.assertIn({"tag": "#priority/medium"}, imported["tags"])
                self.assertEqual(imported["notes"], [{"note": "Comment: Accepted at ExampleConf 2026"}])

    def test_target_change_aborts_before_later_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            papers = []
            for number in range(1, 4):
                pdf = root / f"paper-{number}.pdf"
                pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
                papers.append({"item": paper(number), "pdf_file": str(pdf)})
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps({"collection": "Route A", "papers": papers}),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                fake.state.rename_target_after_attachments = 1
                args = self.make_args(manifest, ledger, fake.base_url)
                args.safety_level = "strict"
                summary = batch.run_batch(args)
                self.assertTrue(summary["aborted"])
                expected_statuses = ["saved_with_pdf", "target_not_found", "not_attempted"]
                for record, expected in zip(summary["results"], expected_statuses, strict=True):
                    with self.subTest(expected=expected):
                        self.assertEqual(record["status"], expected)
                self.assertEqual(len(fake.state.parents), 1)

    def test_balanced_mode_resolves_collection_once_for_the_batch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            papers = []
            for number in range(1, 4):
                pdf = root / f"paper-{number}.pdf"
                pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
                papers.append({"item": paper(number), "pdf_file": str(pdf)})
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(json.dumps({"collection": "Route A", "papers": papers}), encoding="utf-8")
            with FakeZoteroServer(root) as fake:
                summary = batch.run_batch(self.make_args(manifest, ledger, fake.base_url))
                self.assertEqual(summary["status"], "complete")
                self.assertEqual(fake.state.collection_lookup_count, 1)
                self.assertEqual(fake.state.target_lookup_count, 2)
                self.assertEqual(fake.state.ping_count, 2)

    def test_remote_pdf_connection_failure_is_fail_soft(self):
        self.assertIn("zotero_connection_error", batch.FATAL_STATUSES)
        self.assertNotIn("pdf_source_connection_error", batch.FATAL_STATUSES)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            with FakeZoteroServer(root) as fake:
                manifest.write_text(
                    json.dumps(
                        {
                            "collection": "Route A",
                            "papers": [
                                {"item": paper(1), "pdf_url": fake.base_url + "/download.pdf"},
                                {"item": paper(2), "pdf_url": fake.base_url + "/reset.pdf"},
                                {"item": paper(3), "pdf_url": fake.base_url + "/download.pdf"},
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                args = self.make_args(manifest, ledger, fake.base_url)
                args.download_attempts = 1
                summary = batch.run_batch(args)
                self.assertFalse(summary["aborted"])
                self.assertEqual(
                    [record["status"] for record in summary["results"]],
                    ["saved_with_pdf", "pdf_source_connection_error", "saved_with_pdf"],
                )
                self.assertEqual(summary["results"][1]["failure_stage"], "pdf_download")
                self.assertEqual(summary["results"][1]["attempts"], 1)
                self.assertEqual(len(fake.state.parents), 2)

    def test_ordered_pdf_sources_fall_back_before_writing_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            with FakeZoteroServer(root) as fake:
                manifest.write_text(
                    json.dumps(
                        {
                            "collection": "Route A",
                            "papers": [
                                {
                                    "item": paper(1),
                                    "pdf_sources": [
                                        {"pdf_url": fake.base_url + "/reset.pdf"},
                                        {"pdf_url": fake.base_url + "/download.pdf", "pdf_title": "Fallback PDF"},
                                    ],
                                }
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                args = self.make_args(manifest, ledger, fake.base_url)
                args.download_attempts = 1
                summary = batch.run_batch(args)
                result = summary["results"][0]
                self.assertEqual(result["status"], "saved_with_pdf")
                self.assertEqual(result["pdf_source_index"], 1)
                self.assertEqual(
                    [attempt["status"] for attempt in result["pdf_source_attempts"]],
                    ["pdf_source_connection_error", "selected"],
                )
                self.assertEqual(fake.state.children[result["item_key"]][0]["data"]["title"], "Fallback PDF")
                self.assertEqual(len(fake.state.parents), 1)

    def test_progress_reports_preflight_write_upload_verify_and_elapsed_time(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps({"collection": "Route A", "papers": [{"item": paper(1), "pdf_file": str(pdf)}]}),
                encoding="utf-8",
            )
            progress_output = io.StringIO()
            with FakeZoteroServer(root) as fake:
                args = self.make_args(manifest, ledger, fake.base_url)
                args.progress = True
                with contextlib.redirect_stderr(progress_output):
                    summary = batch.run_batch(args)
            events = [json.loads(line) for line in progress_output.getvalue().splitlines()]
            event_names = [event["event"] for event in events]
            for expected in (
                "batch_preflight_started",
                "batch_preflight_finished",
                "paper_started",
                "saving_metadata",
                "assigning_collection",
                "uploading_attachment",
                "verifying_stored_file",
                "paper_finished",
            ):
                self.assertIn(expected, event_names)
            finished = next(event for event in events if event["event"] == "paper_finished")
            self.assertEqual(finished["position"], 1)
            self.assertEqual(finished["total"], 1)
            self.assertGreaterEqual(finished["elapsed_seconds"], 0)
            self.assertEqual(summary["results"][0]["elapsed_seconds"], finished["elapsed_seconds"])

    def test_invalid_manifest_entry_is_isolated_and_later_papers_continue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            papers = []
            for number in (1, 3):
                pdf = root / f"paper-{number}.pdf"
                pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
                papers.append({"item": paper(number), "pdf_file": str(pdf)})
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "collection": "Route A",
                        "papers": [
                            papers[0],
                            {"id": "bad-entry", "item": paper(2), "pdf_fallback_urls": ["typo"]},
                            papers[1],
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                summary = batch.run_batch(self.make_args(manifest, ledger, fake.base_url))
                self.assertEqual(
                    [record["status"] for record in summary["results"]],
                    ["saved_with_pdf", "invalid_manifest_entry", "saved_with_pdf"],
                )
                self.assertIn("unknown keys", summary["results"][1]["message"])
                self.assertEqual(len(fake.state.parents), 2)

    def test_three_resumes_preserve_original_success_and_task_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps({"collection": "Route A", "papers": [{"item": paper(1), "pdf_file": str(pdf)}]}),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                existing_key = fake.state.add_existing(paper(1))
                args = self.make_args(manifest, ledger, fake.base_url)
                first = batch.run_batch(args)
                second = batch.run_batch(args)
                third = batch.run_batch(args)

                self.assertEqual(first["counts"], {"saved_with_pdf": 1})
                self.assertEqual(second["this_run_counts"], {"skipped_completed": 1})
                self.assertEqual(third["this_run_counts"], {"skipped_completed": 1})
                self.assertEqual(third["counts"], {"saved_with_pdf": 1})
                self.assertEqual(third["possible_duplicate_items"], 1)
                effective = third["task_results"][0]
                self.assertEqual(effective["status"], "saved_with_pdf")
                self.assertEqual(effective["possible_duplicate_keys"], [existing_key])
                self.assertTrue(effective["pdf_verified"])
                self.assertTrue(effective["resumed_from_ledger"])
                ledger_records = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(ledger_records), 3)
                self.assertEqual(ledger_records[-1]["original_record"]["status"], "saved_with_pdf")
                self.assertNotIn("original_record", ledger_records[-1]["original_record"])
                self.assertEqual(len(fake.state.parents), 2)

    def test_resume_scope_does_not_cross_collections(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            payload = {"collection": "Route A", "papers": [{"item": paper(1), "pdf_file": str(pdf)}]}
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            with FakeZoteroServer(root) as fake:
                args = self.make_args(manifest, ledger, fake.base_url)
                batch.run_batch(args)
                fake.state.targets[0]["name"] = "Route B"
                fake.state.collections[0]["data"]["name"] = "Route B"
                payload["collection"] = "Route B"
                manifest.write_text(json.dumps(payload), encoding="utf-8")
                second = batch.run_batch(args)
                self.assertEqual(second["results"][0]["status"], "saved_with_pdf")
                self.assertEqual(len(fake.state.parents), 2)

    def test_fully_completed_resume_does_not_require_zotero_online(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps({"collection": "Route A", "papers": [{"item": paper(1), "pdf_file": str(pdf)}]}),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                args = self.make_args(manifest, ledger, fake.base_url)
                batch.run_batch(args)
            resumed = batch.run_batch(args)
            self.assertEqual(resumed["status"], "complete")
            self.assertEqual(resumed["this_run_counts"], {"skipped_completed": 1})
            self.assertEqual(resumed["counts"], {"saved_with_pdf": 1})

    def test_historical_failure_is_reported_after_successful_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_text("not a pdf", encoding="utf-8")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps({"collection": "Route A", "papers": [{"item": paper(1), "pdf_file": str(pdf)}]}),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                args = self.make_args(manifest, ledger, fake.base_url)
                first = batch.run_batch(args)
                self.assertEqual(first["results"][0]["status"], "invalid_pdf")
                pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
                second = batch.run_batch(args)
                self.assertEqual(second["status"], "complete")
                self.assertEqual(second["historical_transient_failures"], 1)
                self.assertEqual(second["historical_failure_items"][0]["status"], "invalid_pdf")
                self.assertEqual(second["currently_unresolved"], [])

    def test_resume_suppresses_duplicate_after_unverified_metadata_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\noriginal content\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps({"collection": "Route A", "papers": [{"item": paper(1), "pdf_file": str(pdf)}]}),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                fake.state.corrupt_attachments = True
                args = self.make_args(manifest, ledger, fake.base_url)
                args.verify_timeout = 0.05
                first = batch.run_batch(args)
                self.assertEqual(first["results"][0]["status"], "verification_failed")
                self.assertEqual(first["needs_action"], 1)
                parent_count = len(fake.state.parents)
                second = batch.run_batch(args)
                self.assertEqual(second["results"][0]["status"], "skipped_needs_review")
                self.assertEqual(second["task_results"][0]["status"], "verification_failed")
                self.assertEqual(second["needs_action"], 1)
                self.assertEqual(len(fake.state.parents), parent_count)

    def test_unexpected_paper_error_is_recorded_and_later_papers_continue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            papers = []
            for number in range(1, 4):
                pdf = root / f"paper-{number}.pdf"
                pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
                papers.append({"item": paper(number), "pdf_file": str(pdf)})
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(json.dumps({"collection": "Route A", "papers": papers}), encoding="utf-8")
            with FakeZoteroServer(root) as fake:
                original_import = single.import_item

                def sometimes_crash(**kwargs):
                    if kwargs["item"]["title"] == "Paper 2":
                        raise RuntimeError("injected crash")
                    return original_import(**kwargs)

                with mock.patch.object(batch.single, "import_item", side_effect=sometimes_crash):
                    summary = batch.run_batch(self.make_args(manifest, ledger, fake.base_url))
                self.assertEqual(
                    [record["status"] for record in summary["results"]],
                    ["saved_with_pdf", "internal_error", "saved_with_pdf"],
                )
                self.assertEqual(summary["results"][1]["failure_stage"], "paper_import")
                self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 3)

    def test_fifty_paper_fault_injection_always_finishes_with_a_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "collection": "Route A",
                        "papers": [
                            {"id": f"paper-{number}", "item": paper(number), "pdf_file": str(pdf)}
                            for number in range(1, 51)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            injected = {
                10: ("pdf_download_timeout", "TimeoutError"),
                20: ("pdf_download_incomplete", "IncompleteRead"),
                30: ("pdf_download_tls_error", "SSLEOFError"),
                40: ("pdf_http_error", "HTTP 404"),
            }

            def simulated_import(**kwargs):
                number = int(kwargs["item"]["title"].split()[-1])
                if number in injected:
                    status, reason = injected[number]
                    raise single.ImportFailure(
                        status,
                        reason,
                        failure_stage="pdf_download",
                        attempts=3 if number != 40 else 1,
                        source_url=f"https://example.test/{number}.pdf",
                    )
                return {
                    "status": "saved_with_pdf",
                    "title": kwargs["item"]["title"],
                    "item_key": f"ITEM{number:04d}",
                    "pdf_verified": True,
                    "possible_duplicate_count": 0,
                    "possible_duplicate_keys": [],
                }

            with FakeZoteroServer(root) as fake, mock.patch.object(
                batch.single,
                "import_item",
                side_effect=simulated_import,
            ):
                summary = batch.run_batch(self.make_args(manifest, ledger, fake.base_url))
            self.assertFalse(summary["aborted"])
            self.assertEqual(len(summary["results"]), 50)
            self.assertEqual(summary["completed"], 46)
            self.assertEqual(summary["failed_or_not_attempted"], 4)
            self.assertEqual(len(ledger.read_text(encoding="utf-8").splitlines()), 50)
            for number, (status, _) in injected.items():
                record = summary["results"][number - 1]
                self.assertEqual(record["status"], status)
                self.assertEqual(record["failure_stage"], "pdf_download")
                self.assertIn("attempts", record)
                self.assertIn("source_url", record)

    def test_stop_on_error_marks_remaining_items_not_attempted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good1 = root / "good1.pdf"
            bad = root / "bad.pdf"
            good3 = root / "good3.pdf"
            good1.write_bytes(b"%PDF-1.4\n%%EOF\n")
            bad.write_text("not a pdf", encoding="utf-8")
            good3.write_bytes(b"%PDF-1.4\n%%EOF\n")
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            manifest.write_text(
                json.dumps(
                    {
                        "collection": "Route A",
                        "papers": [
                            {"item": paper(1), "pdf_file": str(good1)},
                            {"item": paper(2), "pdf_file": str(bad)},
                            {"item": paper(3), "pdf_file": str(good3)},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with FakeZoteroServer(root) as fake:
                args = self.make_args(manifest, ledger, fake.base_url)
                args.stop_on_error = True
                summary = batch.run_batch(args)
                self.assertTrue(summary["aborted"])
                expected_statuses = ["saved_with_pdf", "invalid_pdf", "not_attempted"]
                for record, expected in zip(summary["results"], expected_statuses, strict=True):
                    with self.subTest(expected=expected):
                        self.assertEqual(record["status"], expected)
                self.assertEqual(len(fake.state.parents), 1)

    def test_lock_prevents_parallel_batch_and_stale_file_is_harmless(self):
        with tempfile.TemporaryDirectory() as temp:
            ledger = Path(temp) / "ledger.jsonl"
            first_lock = batch.acquire_lock(ledger)
            try:
                with self.assertRaises(single.ImportFailure) as raised:
                    batch.acquire_lock(ledger)
                self.assertEqual(raised.exception.status, "batch_locked")
            finally:
                batch.release_lock(first_lock)

            self.assertTrue(first_lock.path.exists())
            second_lock = batch.acquire_lock(ledger)
            batch.release_lock(second_lock)

    def test_main_exit_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = root / "good.pdf"
            bad = root / "bad.pdf"
            good.write_bytes(b"%PDF-1.4\n%%EOF\n")
            bad.write_text("not a pdf", encoding="utf-8")
            with FakeZoteroServer(root) as fake:
                cases = [
                    ("complete", good, 0),
                    ("issues", bad, 3),
                ]
                for label, pdf, expected in cases:
                    manifest = root / f"{label}.json"
                    ledger = root / f"{label}.jsonl"
                    manifest.write_text(
                        json.dumps(
                            {
                                "collection": "Route A",
                                "papers": [{"item": paper(1), "pdf_file": str(pdf)}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    with self.subTest(label=label), contextlib.redirect_stdout(io.StringIO()):
                        code = batch.main(
                            [
                                "--manifest",
                                str(manifest),
                                "--ledger",
                                str(ledger),
                                "--base-url",
                                fake.base_url,
                                "--verify-timeout",
                                "2",
                            ]
                        )
                        self.assertEqual(code, expected)

                with self.subTest(label="fatal"), contextlib.redirect_stdout(io.StringIO()):
                    code = batch.main(
                        ["--manifest", str(root / "missing.json"), "--base-url", fake.base_url]
                    )
                    self.assertNotIn(code, {0, 3})

    def test_main_writes_machine_readable_summary_after_unexpected_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            summary_path = root / "summary.json"
            manifest.write_text(json.dumps({"collection": "Route A", "papers": [{}]}), encoding="utf-8")
            output = io.StringIO()
            with mock.patch.object(batch, "run_batch", side_effect=RuntimeError("injected top-level crash")):
                with contextlib.redirect_stdout(output):
                    code = batch.main(
                        [
                            "--manifest",
                            str(manifest),
                            "--ledger",
                            str(ledger),
                            "--summary-json",
                            str(summary_path),
                        ]
                    )
            self.assertEqual(code, 70)
            printed = json.loads(output.getvalue())
            stored = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(printed["status"], "internal_error")
            self.assertEqual(stored["status"], "internal_error")
            self.assertEqual(stored["failure_stage"], "batch_import")

    def test_summary_write_failure_preserves_completed_results_on_stdout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "papers.json"
            ledger = root / "ledger.jsonl"
            summary_path = root / "summary.json"
            manifest.write_text(json.dumps({"collection": "Route A", "papers": [{}]}), encoding="utf-8")
            completed_summary = {
                "status": "complete",
                "aborted": False,
                "results": [{"status": "saved_with_pdf", "item_key": "ITEM0001"}],
            }
            output = io.StringIO()
            with mock.patch.object(batch, "run_batch", return_value=completed_summary), mock.patch.object(
                batch,
                "write_summary_file",
                side_effect=PermissionError("read-only destination"),
            ), contextlib.redirect_stdout(output):
                code = batch.main(
                    [
                        "--manifest",
                        str(manifest),
                        "--ledger",
                        str(ledger),
                        "--summary-json",
                        str(summary_path),
                        "--no-progress",
                    ]
                )
            printed = json.loads(output.getvalue())
            self.assertEqual(code, 2)
            self.assertEqual(printed["status"], "complete")
            self.assertEqual(printed["results"][0]["status"], "saved_with_pdf")
            self.assertIn("read-only destination", printed["summary_write_error"])


if __name__ == "__main__":
    unittest.main()
