from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_DIR = ROOT / "save-papers-to-zotero" / "scripts"
SUMMARY_SCRIPT = SCRIPT_DIR / "zotero_summary_note.py"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(ROOT / "tests"))

import zotero_summary_note as summary_note  # noqa: E402
from test_importers import FakeZoteroServer, paper  # noqa: E402


def valid_summary() -> dict:
    return {
        "summary": "论文提出“模块 ⋆”与 <script>alert('x')</script> 方法，并报告 A & B。",
        "background": "现有方法难以处理长上下文。",
        "problems": "现有方法缺少长上下文证据，并且评测覆盖不足。",
        "methodology": "作者使用 Dataset-X，并与 Baseline-Y 比较。",
        "results": "准确率由 81.2% 提升到 84.7%。",
        "focus": [
            {
                "location": "Section 3.1（<Architecture>）· 第 2 段（公式 1-2）",
                "reason": "理解主要模型结构。",
            },
            {"location": "Table 2（消融实验）", "reason": "核对实验设置与结果。"},
        ],
    }


class SummaryRenderTests(unittest.TestCase):
    def test_render_escapes_untrusted_text_and_uses_fixed_structure(self):
        note = summary_note.render_summary_note(valid_summary())
        self.assertIn("<h1>AI 论文导读</h1>", note)
        for heading in summary_note.SUMMARY_HEADINGS:
            self.assertIn(heading, note)
        self.assertNotIn("<script>", note)
        self.assertIn("&lt;script&gt;", note)
        self.assertIn("A &amp; B", note)
        self.assertIn("Section 3.1（&lt;Architecture&gt;）", note)
        self.assertIn("“模块 ⋆”", note)
        self.assertNotIn("PDF 页码", note)
        self.assertIn(
            "<li><strong>Table 2（消融实验）</strong>：核对实验设置与结果。</li>",
            note,
        )

    def test_schema_requires_exact_fields_and_two_to_four_focus_entries(self):
        cases = []
        missing = valid_summary()
        missing.pop("results")
        cases.append(missing)
        missing_problems = valid_summary()
        missing_problems.pop("problems")
        cases.append(missing_problems)
        unexpected = valid_summary()
        unexpected["html"] = "<p>unsafe</p>"
        cases.append(unexpected)
        short_focus = valid_summary()
        short_focus["focus"] = short_focus["focus"][:1]
        cases.append(short_focus)
        long_focus = valid_summary()
        long_focus["focus"] = long_focus["focus"] * 3
        cases.append(long_focus)
        legacy_focus = valid_summary()
        legacy_focus["focus"][0] = {
            "section": "3 Method",
            "pages": "5-7",
            "reason": "旧 schema。",
        }
        cases.append(legacy_focus)
        empty_location = valid_summary()
        empty_location["focus"][0]["location"] = "   "
        cases.append(empty_location)

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(summary_note.SummaryFailure) as raised:
                    summary_note.validate_summary(payload)
                self.assertEqual(raised.exception.status, "invalid_summary_json")

    def test_render_command_writes_utf8_html_and_structured_result(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "paper.summary.json"
            output = root / "paper.summary.html"
            source.write_text(json.dumps(valid_summary(), ensure_ascii=False), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                exit_code = summary_note.main(
                    ["render", "--summary-json", str(source), "--output", str(output)]
                )
            result = json.loads(stdout.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(result["status"], "summary_rendered")
            self.assertEqual(result["focus_count"], 2)
            self.assertIn("AI 论文导读", output.read_text(encoding="utf-8"))

    def test_render_cli_forces_utf8_without_x_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "论文⋆.summary.json"
            output = root / "导读⋆.html"
            source.write_text(json.dumps(valid_summary(), ensure_ascii=False), encoding="utf-8")
            environment = os.environ.copy()
            environment.pop("PYTHONIOENCODING", None)
            environment.pop("PYTHONUTF8", None)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(SUMMARY_SCRIPT),
                    "render",
                    "--summary-json",
                    str(source),
                    "--output",
                    str(output),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                timeout=10,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr.decode("utf-8"))
            result = json.loads(completed.stdout.decode("utf-8"))
            self.assertEqual(result["status"], "summary_rendered")
            self.assertIn("导读⋆.html", result["note_file"])
            self.assertIn("“模块 ⋆”", output.read_text(encoding="utf-8"))


class SummaryVerificationTests(unittest.TestCase):
    def test_summary_note_flows_through_import_then_separate_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
            with FakeZoteroServer(root) as fake:
                note = summary_note.render_summary_note(valid_summary())
                imported = summary_note.single.import_item(
                    item=paper(1),
                    collection="Route A",
                    pdf_file=pdf,
                    notes=[note],
                    base_url=fake.base_url,
                    verify_timeout=2,
                )
                self.assertEqual(imported["status"], "saved_with_pdf")
                self.assertEqual(fake.state.note_children_queries, 0)

                verified = summary_note.verify_summary_note(
                    fake.base_url, imported["item_key"], 1
                )
                self.assertEqual(verified["status"], "note_verified")
                self.assertEqual(verified["item_key"], imported["item_key"])
                self.assertEqual(fake.state.note_children_queries, 1)

    def test_verify_failure_is_separate_from_pdf_import_status(self):
        with tempfile.TemporaryDirectory() as temp:
            with FakeZoteroServer(Path(temp)) as fake:
                item_key = fake.state.add_existing({"title": "Paper"}, with_pdf=True)
                result = summary_note.verify_summary_note(fake.base_url, item_key, 0.01)
                self.assertEqual(result["status"], "note_verification_failed")
                self.assertEqual(result["note_count"], 0)

    def test_verify_cli_forces_utf8_without_x_utf8(self):
        with tempfile.TemporaryDirectory() as temp:
            with FakeZoteroServer(Path(temp)) as fake:
                item_key = fake.state.add_existing({"title": "Paper"}, with_pdf=True)
                environment = os.environ.copy()
                environment.pop("PYTHONIOENCODING", None)
                environment.pop("PYTHONUTF8", None)
                completed = subprocess.run(
                    [
                        sys.executable,
                        str(SUMMARY_SCRIPT),
                        "verify",
                        "--item-key",
                        item_key,
                        "--timeout",
                        "0.01",
                        "--base-url",
                        fake.base_url,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=environment,
                    timeout=10,
                    check=False,
                )
                self.assertEqual(completed.returncode, 3, completed.stderr.decode("utf-8"))
                result = json.loads(completed.stdout.decode("utf-8"))
                self.assertEqual(result["status"], "note_verification_failed")
                self.assertEqual(result["marker"], "AI 论文导读")

    def test_invalid_item_key_is_rejected_before_api_call(self):
        with self.assertRaises(summary_note.SummaryFailure) as raised:
            summary_note.verify_summary_note("http://127.0.0.1:1", "../bad", 1)
        self.assertEqual(raised.exception.status, "invalid_item_key")


if __name__ == "__main__":
    unittest.main()
