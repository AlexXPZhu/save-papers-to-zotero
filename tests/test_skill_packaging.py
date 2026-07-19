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
            SKILL_DIR / "scripts" / "zotero_connector_import.py",
            SKILL_DIR / "scripts" / "zotero_batch_import.py",
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
        for filename in ("README.md", "README.zh-CN.md"):
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
                self.assertIn("claude --plugin-dir ./save-papers-to-zotero --chrome", text)

    def test_connector_session_identifier_is_platform_neutral(self) -> None:
        importer = (
            SKILL_DIR / "scripts" / "zotero_connector_import.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('session_id = "codex-', importer)
        self.assertIn('session_id = "zotero-import-', importer)


if __name__ == "__main__":
    unittest.main()
