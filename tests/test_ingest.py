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
import zotero_ingest as ingest  # noqa: E402

from test_importers import FakeZoteroServer  # noqa: E402  (reuse fixtures)


# ----------------------------------------------------------- fake public APIs


class FakeApi:
    """Serves canned Crossref JSON, arXiv Atom XML, and PDF bytes."""

    def __init__(self):
        self.crossref: dict[str, dict] = {}
        self.arxiv: dict[str, str] = {}
        self.arxiv_missing: set[str] = set()
        self.requests: list[str] = []

    def handler(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send(self, status: int, body: bytes, content_type: str):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                state.requests.append(path)
                if path.startswith("/works/"):
                    doi = urllib.parse.unquote(path[len("/works/") :])
                    message = state.crossref.get(doi)
                    if message is None:
                        self._send(404, b"not found", "text/plain")
                    else:
                        body = json.dumps({"message": message}).encode("utf-8")
                        self._send(200, body, "application/json")
                    return
                if path == "/api/query":
                    id_list = urllib.parse.parse_qs(parsed.query).get("id_list", [""])[0]
                    if id_list in state.arxiv_missing:
                        self._send(404, b"not found", "text/plain")
                        return
                    xml = state.arxiv.get(id_list)
                    if xml is None:
                        xml = (
                            '<?xml version="1.0" encoding="UTF-8"?>'
                            '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
                        )
                    self._send(200, xml.encode("utf-8"), "application/atom+xml")
                    return
                if path.startswith("/pdf/"):
                    self._send(200, b"%PDF-1.4\n%%EOF\n", "application/pdf")
                    return
                if path.startswith("/abs/"):
                    self._send(200, b"<html></html>", "text/html")
                    return
                self._send(404, b"not found", "text/plain")

        return Handler


class FakeApiServer:
    def __init__(self):
        self.state = FakeApi()
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


# --------------------------------------------------------------- canned data


def crossref_message(
    doi: str,
    *,
    title: str = "Crossref Paper",
    cr_type: str = "journal-article",
    page: str = "1–15",
) -> dict:
    return {
        "type": cr_type,
        "title": [title],
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "container-title": ["Journal of Examples"],
        "published-print": {"date-parts": [[2026, 3]]},
        "DOI": doi,
        "URL": f"https://example.test/{doi}",
        "volume": "12",
        "issue": "3",
        "page": page,
        "publisher": "Example Publisher",
        "language": "en",
        "abstract": "<jats:p>An example abstract.</jats:p>",
        "ISSN": ["1234-5678"],
    }


def atom_xml(
    arxiv_id: str,
    *,
    title: str = "arXiv Paper",
    comment: str | None = "Accepted at ExampleConf 2024",
    doi: str | None = "10.1000/arxiv-1",
) -> str:
    doi_el = f"<arxiv:doi>{doi}</arxiv:doi>" if doi else ""
    comment_el = f"<arxiv:comment>{comment}</arxiv:comment>" if comment else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:arxiv="http://arxiv.org/schemas/atom">'
        f"<entry>"
        f"<id>http://arxiv.org/abs/{arxiv_id}v1</id>"
        f"<title>{title}</title>"
        f"<summary>An arXiv abstract.</summary>"
        f"<author><name>Ada Lovelace</name></author>"
        f"<published>2024-01-05T00:00:00Z</published>"
        f"{doi_el}{comment_el}"
        f"</entry></feed>"
    )


# ------------------------------------------------------------------- helpers


def ingest_args(
    *,
    identifiers: str | None = None,
    bibtex: str | None = None,
    collection: str = "Route A",
    out: str | Path | None = None,
    report: str | Path | None = None,
    crossref_base: str | None = None,
    arxiv_base: str | None = None,
    extra: list[str] | None = None,
) -> argparse.Namespace:
    argv: list[str] = []
    if identifiers is not None:
        argv += ["--identifiers", identifiers]
    if bibtex is not None:
        argv += ["--bibtex", bibtex]
    argv += ["--collection", collection]
    if out is not None:
        argv += ["--out", str(out)]
    if report is not None:
        argv += ["--report", str(report)]
    if crossref_base is not None:
        argv += ["--crossref-base", crossref_base]
    if arxiv_base is not None:
        argv += ["--arxiv-base", arxiv_base]
    argv += ["--delay", "0", "--http-timeout", "5"]
    argv += extra or []
    return ingest.build_parser().parse_args(argv)


def make_batch_args(manifest: Path, ledger: Path, base_url: str, **overrides) -> argparse.Namespace:
    ns = argparse.Namespace(
        manifest=manifest,
        collection=None,
        target_id=None,
        ledger=ledger,
        summary_json=None,
        download_timeout=5,
        verify_timeout=2,
        dry_run=False,
        stop_on_error=False,
        resume=True,
        base_url=base_url,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


# =================================================================== tests


class ClassifyIdentifierTests(unittest.TestCase):
    def test_classify_identifier(self):
        cases = [
            ("10.1000/paper-1", ("doi", "10.1000/paper-1")),
            ("https://doi.org/10.1145/3290605.3300233", ("doi", "10.1145/3290605.3300233")),
            ("doi: 10.1000/abc", ("doi", "10.1000/abc")),
            ("arXiv:2401.00001", ("arxiv", "2401.00001")),
            ("https://arxiv.org/abs/2401.00001v2", ("arxiv", "2401.00001")),
            ("arxiv.org/pdf/cs.AI/9701001", ("arxiv", "cs.ai/9701001")),
            ("2401.00001", ("arxiv", "2401.00001")),
            ("2401.00001v3", ("arxiv", "2401.00001")),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(ingest.classify_identifier(raw), expected)

        # Bare digits and unrecognized text do not get guessed.
        self.assertIsNone(ingest.classify_identifier("12345"))
        self.assertIsNone(ingest.classify_identifier("garbage"))
        self.assertEqual(
            ingest.classify_identifier("https://example.com/something"),
            ("url", "https://example.com/something"),
        )


class CrossrefMappingTests(unittest.TestCase):
    def test_crossref_type_mapping(self):
        for cr_type, expected in [
            ("preprint", "preprint"),
            ("dissertation", "thesis"),
            ("journal-article", "journalArticle"),
            ("posted-content", "preprint"),
            ("unknown-type", "journalArticle"),
        ]:
            with self.subTest(cr_type=cr_type):
                item = ingest.crossref_to_item(
                    {"type": cr_type, "title": "T", "DOI": "10.1/x"}, "10.1/x"
                )
                self.assertEqual(item["itemType"], expected)

    def test_crossref_response_maps_to_translator_item(self):
        with FakeApiServer() as fake:
            fake.state.crossref["10.1000/paper-1"] = crossref_message("10.1000/paper-1")
            resolved = ingest.fetch_crossref(
                "10.1000/paper-1",
                mailto="test@example.com",
                timeout=5,
                base_url=fake.base_url,
            )
        item = resolved["item"]
        self.assertEqual(item["itemType"], "journalArticle")
        self.assertEqual(item["title"], "Crossref Paper")
        self.assertEqual(
            item["creators"],
            [{"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"}],
        )
        self.assertEqual(item["publicationTitle"], "Journal of Examples")
        self.assertEqual(item["pages"], "1-15")  # en dash normalized
        self.assertEqual(item["date"], "2026-03")
        self.assertEqual(item["abstractNote"], "An example abstract.")  # tags stripped
        self.assertIsNone(resolved["pdf_url"])
        self.assertEqual(resolved["source_url"], "https://doi.org/10.1000/paper-1")


class ArxivMappingTests(unittest.TestCase):
    def test_arxiv_response_maps_to_item_with_comment_and_pdf(self):
        with FakeApiServer() as fake:
            base = fake.base_url
            fake.state.arxiv["2401.00001"] = atom_xml("2401.00001")
            resolved = ingest.fetch_arxiv("2401.00001", timeout=5, base_url=base)
        item = resolved["item"]
        self.assertEqual(item["itemType"], "preprint")
        self.assertEqual(item["title"], "arXiv Paper")
        self.assertEqual(
            item["creators"], [{"creatorType": "author", "name": "Ada Lovelace"}]
        )
        self.assertEqual(item["archiveID"], "arXiv:2401.00001")
        self.assertEqual(item["DOI"], "10.1000/arxiv-1")
        self.assertEqual(item["date"], "2024-01")
        self.assertEqual(resolved["arxiv_comment"], "Accepted at ExampleConf 2024")
        self.assertEqual(resolved["pdf_url"], f"{base}/pdf/2401.00001")
        self.assertEqual(resolved["source_url"], f"{base}/abs/2401.00001")
        self.assertEqual(resolved["referrer"], f"{base}/abs/2401.00001")


class BibtexParserTests(unittest.TestCase):
    BIB = (
        "@string{ignored = \"macro\"}\n"
        "@article{lovelace2026,\n"
        "  title = {An Example Paper},\n"
        "  author = {Lovelace, Ada and Babbage, Charles},\n"
        "  journal = {Journal of Examples},\n"
        "  volume = {12},\n"
        "  number = {3},\n"
        "  pages = {1--15},\n"
        "  year = {2026},\n"
        "  doi = {10.1000/paper-1},\n"
        "  keywords = {llm; survey, vision},\n"
        "}\n"
        "@inproceedings{babbage2026,\n"
        "  title = {A Conference Paper},\n"
        "  author = {Charles Babbage},\n"
        "  booktitle = {Proc. Example},\n"
        "  pages = {1–2},\n"
        "}\n"
        "@misc{smith2024,\n"
        "  title = {A Preprint},\n"
        "  author = {John Q. Smith},\n"
        "  eprint = {2401.00002},\n"
        "  archivePrefix = {arXiv},\n"
        "  abstract = {An abstract.},\n"
        "  note = {Read carefully.},\n"
        "}\n"
    )

    def test_bibtex_article_and_inproceedings_parse(self):
        entries, unresolved, duplicates = ingest.resolve_bibtex(
            self.BIB, arxiv_base="https://arxiv.org"
        )
        self.assertEqual(unresolved, [])
        self.assertEqual(duplicates, [])
        by_id = {entry["id"]: entry for entry in entries}
        self.assertEqual(set(by_id), {"lovelace2026", "babbage2026", "smith2024"})

        article = by_id["lovelace2026"]["item"]
        self.assertEqual(article["itemType"], "journalArticle")
        self.assertEqual(
            article["creators"],
            [
                {"creatorType": "author", "firstName": "Ada", "lastName": "Lovelace"},
                {"creatorType": "author", "firstName": "Charles", "lastName": "Babbage"},
            ],
        )
        self.assertEqual(article["publicationTitle"], "Journal of Examples")
        self.assertEqual(article["issue"], "3")
        self.assertEqual(article["pages"], "1-15")  # TeX -- normalized
        self.assertEqual(by_id["lovelace2026"]["tags"], ["llm", "survey", "vision"])
        self.assertEqual(by_id["lovelace2026"]["source_url"], "https://doi.org/10.1000/paper-1")

        inproc = by_id["babbage2026"]["item"]
        self.assertEqual(inproc["itemType"], "conferencePaper")
        self.assertEqual(inproc["proceedingsTitle"], "Proc. Example")
        self.assertEqual(inproc["pages"], "1-2")  # en dash normalized

    def test_bibtex_eprint_arxiv_sets_pdf_url(self):
        entries, _unresolved, _duplicates = ingest.resolve_bibtex(
            self.BIB, arxiv_base="https://arxiv.org"
        )
        misc = next(e for e in entries if e["id"] == "smith2024")
        self.assertEqual(misc["item"]["itemType"], "preprint")
        self.assertEqual(misc["item"]["archiveID"], "arXiv:2401.00002")
        self.assertEqual(misc["pdf_url"], "https://arxiv.org/pdf/2401.00002")
        self.assertEqual(misc["referrer"], "https://arxiv.org/abs/2401.00002")
        self.assertEqual(misc["notes"], ["Read carefully."])

    def test_bibtex_string_comment_preamble_are_skipped(self):
        text = (
            '@comment{This is a comment with a {nested} brace}\n'
            '@string{j = {Journal}}\n'
            '@preamble{"latex command"}\n'
            '@article{real2026, title = {Real}, journal = {J},}\n'
        )
        entries, unresolved, _duplicates = ingest.resolve_bibtex(
            text, arxiv_base="https://arxiv.org"
        )
        self.assertEqual([e["id"] for e in entries], ["real2026"])
        self.assertEqual(unresolved, [])


class IngestFlowTests(unittest.TestCase):
    def test_ingest_continues_after_unresolved_identifier(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root / "ids.txt"
            ids.write_text("10.1000/paper-1\n9999.99999\n", encoding="utf-8")
            out = root / "out.manifest.json"
            with FakeApiServer() as api:
                api.state.crossref["10.1000/paper-1"] = crossref_message("10.1000/paper-1")
                api.state.arxiv_missing.add("9999.99999")
                summary = ingest.run(
                    ingest_args(
                        identifiers=str(ids),
                        collection="Route A",
                        out=out,
                        crossref_base=api.base_url,
                        arxiv_base=api.base_url,
                    )
                )
            self.assertEqual(summary["status"], "completed_with_issues")
            self.assertEqual(summary["resolved"], 1)
            self.assertEqual(summary["unresolved"], 1)
            self.assertEqual(summary["needs_pdf"], 1)
            self.assertEqual(summary["with_pdf"], 0)
            self.assertTrue(out.exists())

    def test_ingest_dedups_repeated_identifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root / "ids.txt"
            ids.write_text("10.1000/paper-1\n10.1000/paper-1\n", encoding="utf-8")
            out = root / "out.manifest.json"
            with FakeApiServer() as api:
                api.state.crossref["10.1000/paper-1"] = crossref_message("10.1000/paper-1")
                summary = ingest.run(
                    ingest_args(
                        identifiers=str(ids),
                        collection="Route A",
                        out=out,
                        crossref_base=api.base_url,
                    )
                )
            self.assertEqual(summary["resolved"], 1)
            self.assertEqual(summary["skipped_duplicate"], 1)
            manifest = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["papers"]), 1)

    def test_ingest_writes_shared_fields_at_top_level_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root / "ids.txt"
            ids.write_text("2401.00001\n", encoding="utf-8")
            out = root / "out.manifest.json"
            with FakeApiServer() as api:
                api.state.arxiv["2401.00001"] = atom_xml("2401.00001")
                summary = ingest.run(
                    ingest_args(
                        identifiers=str(ids),
                        collection="Route A",
                        out=out,
                        arxiv_base=api.base_url,
                        extra=[
                            "--tag", "shared",
                            "--reading-status", "reading",
                            "--priority", "medium",
                        ],
                    )
                )
            self.assertEqual(summary["resolved"], 1)
            manifest = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(manifest["tags"], ["shared"])
            self.assertEqual(manifest["reading_status"], "reading")
            self.assertEqual(manifest["priority"], "medium")
            # Shared fields are not baked into the single paper entry.
            self.assertNotIn("tags", manifest["papers"][0])
            self.assertNotIn("reading_status", manifest["papers"][0])
            self.assertNotIn("priority", manifest["papers"][0])

    def test_ingest_manifest_feeds_batch_dry_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root / "ids.txt"
            ids.write_text("10.1000/paper-1\n2401.00001\n", encoding="utf-8")
            manifest = root / "out.manifest.json"
            ledger = root / "ledger.jsonl"
            with FakeApiServer() as api, FakeZoteroServer(root) as fake:
                api.state.crossref["10.1000/paper-1"] = crossref_message("10.1000/paper-1")
                api.state.arxiv["2401.00001"] = atom_xml("2401.00001")
                summary = ingest.run(
                    ingest_args(
                        identifiers=str(ids),
                        collection="Route A",
                        out=manifest,
                        crossref_base=api.base_url,
                        arxiv_base=api.base_url,
                    )
                )
                self.assertEqual(summary["resolved"], 2)
                # Mixed manifest is parseable without PDF requirement (dry-run).
                _collection, _target_id, requests = batch.parse_manifest(
                    manifest, None, None, require_pdf=False
                )
                self.assertEqual(len(requests), 2)
                args = make_batch_args(manifest, ledger, fake.base_url, dry_run=True)
                result = batch.run_batch(args)
            self.assertEqual(result["status"], "complete")
            for record in result["results"]:
                self.assertEqual(record["status"], "ready")
            self.assertEqual(len(fake.state.parents), 0)

    def test_ingest_arxiv_manifest_imports_with_pdf(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root / "ids.txt"
            ids.write_text("2401.00001\n", encoding="utf-8")
            manifest = root / "out.manifest.json"
            ledger = root / "ledger.jsonl"
            with FakeApiServer() as api, FakeZoteroServer(root) as fake:
                api.state.arxiv["2401.00001"] = atom_xml("2401.00001")
                summary = ingest.run(
                    ingest_args(
                        identifiers=str(ids),
                        collection="Route A",
                        out=manifest,
                        arxiv_base=api.base_url,
                    )
                )
                self.assertEqual(summary["resolved"], 1)
                self.assertEqual(summary["with_pdf"], 1)
                args = make_batch_args(manifest, ledger, fake.base_url)
                result = batch.run_batch(args)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["results"][0]["status"], "saved_with_pdf")
            self.assertEqual(len(fake.state.parents), 1)

    def test_ingest_mixed_manifest_reports_needs_pdf_as_isolated_entry_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root / "ids.txt"
            ids.write_text("2401.00001\n10.1000/paper-1\n", encoding="utf-8")
            out = root / "out.manifest.json"
            with FakeApiServer() as api:
                api.state.crossref["10.1000/paper-1"] = crossref_message("10.1000/paper-1")
                api.state.arxiv["2401.00001"] = atom_xml("2401.00001")
                summary = ingest.run(
                    ingest_args(
                        identifiers=str(ids),
                        collection="Route A",
                        out=out,
                        crossref_base=api.base_url,
                        arxiv_base=api.base_url,
                    )
                )
            self.assertEqual(summary["with_pdf"], 1)
            self.assertEqual(summary["needs_pdf"], 1)
            _, _, requests = batch.parse_manifest(out, None, None, require_pdf=True)
            self.assertIsNone(requests[0].parse_error)
            self.assertIsNotNone(requests[1].parse_error)
            self.assertEqual(requests[1].parse_error.status, "invalid_manifest")
            self.assertIn("requires exactly one", requests[1].parse_error.message)

    def test_ingest_all_unresolved_returns_nonzero_and_no_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ids = root / "ids.txt"
            ids.write_text("9999.99999\ngarbage\n", encoding="utf-8")
            out = root / "out.manifest.json"
            report = root / "report.json"
            with FakeApiServer() as api:
                api.state.arxiv_missing.add("9999.99999")
                summary = ingest.run(
                    ingest_args(
                        identifiers=str(ids),
                        collection="Route A",
                        out=out,
                        report=report,
                        arxiv_base=api.base_url,
                    )
                )
                with contextlib.redirect_stdout(io.StringIO()):
                    code = ingest.main(
                        [
                            "--identifiers", str(ids),
                            "--collection", "Route A",
                            "--out", str(out),
                            "--arxiv-base", api.base_url,
                            "--delay", "0",
                        ]
                    )
            self.assertEqual(summary["status"], "failed")
            self.assertEqual(summary["resolved"], 0)
            self.assertIsNone(summary["manifest"])
            self.assertFalse(out.exists())
            self.assertTrue(report.exists())  # report is still written
            self.assertNotEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
