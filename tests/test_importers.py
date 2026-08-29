from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT_DIR = (
    Path(__file__).resolve().parents[1]
    / "save-papers-to-zotero"
    / "scripts"
)
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

    def _add_pdf(self, parent_key: str, title: str) -> str:
        key = f"PDF{self.next_attachment:05d}"
        self.next_attachment += 1
        path = self.root / f"{key}.pdf"
        path.write_bytes(b"%PDF-1.4\n%%EOF\n")
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
                    self.send_json(200, {}, X_Zotero_Version="9.0-test")
                    return
                if parsed.path == "/connector/getSelectedCollection":
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
                    state._add_pdf(session["key"], metadata["title"])
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
                if parsed.path == "/api/users/0/collections":
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


class BatchImporterTests(unittest.TestCase):
    def make_args(self, manifest: Path, ledger: Path, base_url: str) -> argparse.Namespace:
        return argparse.Namespace(
            manifest=manifest,
            collection=None,
            target_id=None,
            ledger=ledger,
            summary_json=None,
            download_timeout=2,
            verify_timeout=2,
            dry_run=False,
            stop_on_error=False,
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
                summary = batch.run_batch(self.make_args(manifest, ledger, fake.base_url))
                self.assertTrue(summary["aborted"])
                expected_statuses = ["saved_with_pdf", "target_not_found", "not_attempted"]
                for record, expected in zip(summary["results"], expected_statuses, strict=True):
                    with self.subTest(expected=expected):
                        self.assertEqual(record["status"], expected)
                self.assertEqual(len(fake.state.parents), 1)

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


if __name__ == "__main__":
    unittest.main()
