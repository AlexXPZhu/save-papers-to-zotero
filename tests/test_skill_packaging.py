import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "save-papers-to-zotero"
SKILL_FILE = SKILL_DIR / "SKILL.md"
MARKETPLACE_FILE = ROOT / ".claude-plugin" / "marketplace.json"


class SkillPackagingTests(unittest.TestCase):
    def test_shared_skill_has_codex_and_claude_resources(self) -> None:
        required = [
            SKILL_FILE,
            SKILL_DIR / "agents" / "openai.yaml",
            SKILL_DIR / "references" / "batch-manifest.md",
            SKILL_DIR / "references" / "ingest.md",
            SKILL_DIR / "references" / "summarization.md",
            SKILL_DIR / "scripts" / "zotero_connector_import.py",
            SKILL_DIR / "scripts" / "zotero_batch_import.py",
            SKILL_DIR / "scripts" / "zotero_ingest.py",
            SKILL_DIR / "scripts" / "zotero_summary_note.py",
        ]
        for path in required:
            with self.subTest(path=path):
                self.assertTrue(path.is_file())

        skill = SKILL_FILE.read_text(encoding="utf-8")
        frontmatter = re.match(r"\A---\n(.*?)\n---\n", skill, re.DOTALL)
        self.assertIsNotNone(frontmatter)
        self.assertIn("name: save-papers-to-zotero", frontmatter.group(1))
        self.assertIn("description:", frontmatter.group(1))
        self.assertIn("${CLAUDE_SKILL_DIR}", skill)
        self.assertIn("Chrome-control skill in Codex", skill)
        self.assertIn("Claude in Chrome", skill)
        self.assertIn("<skill-dir>/scripts/zotero_connector_import.py", skill)
        self.assertIn("<skill-dir>/scripts/zotero_ingest.py", skill)
        self.assertIn("<skill-dir>/scripts/zotero_summary_note.py", skill)
        self.assertIn("Ingest from Identifier Lists or BibTeX", skill)
        self.assertIn("Generate Optional Chinese Summaries", skill)

        summary_reference = (SKILL_DIR / "references" / "summarization.md").read_text(
            encoding="utf-8"
        )
        for heading in (
            "概述",
            "背景与动机",
            "解决的核心问题",
            "方法与架构",
            "成果与评估",
            "重点阅读",
        ):
            self.assertIn(heading, summary_reference)
        self.assertIn("needs_ocr", summary_reference)
        self.assertIn("HTML-escapes", summary_reference)
        self.assertIn("https://arxiv.org/html/<arXiv-id>", summary_reference)
        self.assertIn("ASCII 双引号", summary_reference)
        self.assertIn('"location"', summary_reference)
        self.assertNotIn('"pages"', summary_reference)

    def test_claude_marketplace_exposes_only_the_shared_skill(self) -> None:
        marketplace = json.loads(MARKETPLACE_FILE.read_text(encoding="utf-8"))
        self.assertEqual("save-papers-to-zotero", marketplace["name"])
        self.assertEqual(1, len(marketplace["plugins"]))

        plugin = marketplace["plugins"][0]
        self.assertEqual("save-papers-to-zotero", plugin["name"])
        self.assertEqual("./save-papers-to-zotero", plugin["source"])
        self.assertIs(plugin["strict"], False)
        self.assertEqual(["./"], plugin["skills"])
        self.assertNotIn("version", plugin)
        self.assertEqual(SKILL_DIR.resolve(), (ROOT / plugin["source"]).resolve())
        self.assertFalse((SKILL_DIR / ".claude-plugin" / "plugin.json").exists())

    def test_docs_cover_both_installation_paths(self) -> None:
        zero_dependency_phrases = {
            "README.md": "Uses Python's standard library only",
            "README.zh-CN.md": "仅使用 Python 标准库",
        }
        for filename, zero_dependency_phrase in zero_dependency_phrases.items():
            text = (ROOT / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("$skill-installer", text)
                self.assertIn(
                    "/plugin marketplace add AlexXPZhu/save-papers-to-zotero", text
                )
                self.assertIn(
                    "/plugin install save-papers-to-zotero@save-papers-to-zotero",
                    text,
                )
                self.assertIn("summarization.md", text)
                self.assertIn(zero_dependency_phrase, text)
                self.assertNotIn("-X utf8", text)
                self.assertIn("claude --plugin-dir ./save-papers-to-zotero --chrome", text)

    def test_connector_session_identifier_is_platform_neutral(self) -> None:
        importer = (
            SKILL_DIR / "scripts" / "zotero_connector_import.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('session_id = "codex-', importer)
        self.assertIn('session_id = "zotero-import-', importer)


if __name__ == "__main__":
    unittest.main()
