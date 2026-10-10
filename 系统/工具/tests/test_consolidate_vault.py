import sys
from collections import Counter
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import consolidate_vault as c


def note(name, data=None, body=""):
    data = {"type": "work", "display_title": name, **(data or {})}
    text = "---\n" + c.yaml.safe_dump(data) + "---\n# " + name + "\n" + body
    return c.parse_note(Path("作品") / (name + ".md"), text)


class ConsolidationTests(unittest.TestCase):
    def test_imported_description_has_no_trailing_whitespace(self):
        self.assertEqual(c.bbcode("First line.  \nSecond line. \n"), "First line.\nSecond line.")

    def test_relation_only_and_cover_are_empty(self):
        item = note("Stub", {"cover_image": "assets/covers/v1.webp"},
                    '<!-- AUTO:BEGIN image -->![[assets/covers/v1.webp]]<!-- AUTO:END image -->\n'
                    '<!-- AUTO:BEGIN relation -->- [[Other]]（fan）<!-- AUTO:END relation -->')
        self.assertTrue(c.empty_work(item))

    def test_facts_and_manual_bullet_prose_are_not_empty(self):
        self.assertFalse(c.empty_work(note("A", {"first_release_date": "2017-01-01"})))
        self.assertFalse(c.empty_work(note("A", body="- A teacher returns to his hometown.")))
        self.assertFalse(c.empty_work(note("A", body='<!-- AUTO:BEGIN tag -->- [[Romance]]<!-- AUTO:END tag -->')))

    def test_complete_and_richer_release_wins(self):
        sparse = note("Sparse", {"edition_type": "complete", "release_date": "2000-01-01"})
        rich = note("Rich", {"edition_type": "complete", "release_date": "2000-01-01", "platform": "win"},
                    '<!-- AUTO:BEGIN duty -->- [[Developer]]<!-- AUTO:END duty -->')
        self.assertGreater(c.release_score(rich, Counter()), c.release_score(sparse, Counter()))
        trial = note("Trial", {"edition_type": "trial", "platform": "win", "release_date": "2000-01-01"})
        self.assertGreater(c.release_score(sparse, Counter()), c.release_score(trial, Counter()))

    def test_release_archive_preserves_previous_merged_ids(self):
        keeper = note("Keep", {"source_ids": {"vndb": "r1"}, "merged_source_records": [{"id": "r0"}]})
        other = note("Other", {"source_ids": {"vndb": "r2"}, "aliases": ["Old"]})
        parsed = c.parse_note(keeper.path, c.consolidated_note(keeper, [other]))
        self.assertEqual({r["id"] for r in parsed.data["consolidated_releases"]}, {"r0", "r1", "r2"})
        self.assertEqual(set(parsed.data["aliases"]), {"Old", "Other"})

    def test_link_redirect_preserves_alias_heading_and_qualifies(self):
        text = '[[发行版/Old#Info|Label]] [[Old]]'
        result = c.rewrite_links(text, Path("作品/A.md"), {"Old": "Keep"}, {}, set())
        self.assertEqual(result, '[[发行版/Keep#Info|Label]] [[发行版/Keep]]')

    def test_collision_does_not_redirect_a_work_link(self):
        text = '[[Same]]\n<!-- AUTO:BEGIN release -->- [[Same]]<!-- AUTO:END release -->'
        result = c.rewrite_links(text, Path("作品/A.md"), {"Same": "Keep"}, {}, {"Same"})
        self.assertTrue(result.startswith('[[Same]]'))
        self.assertIn('[[发行版/Keep]]', result)

    def test_empty_deletion_requires_definitive_result(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "作品").mkdir()
            item = note("Stub")
            path = root / item.path
            path.write_text(item.text, encoding="utf-8")
            item = c.parse_note(path, item.text)
            scanned = {"groups": {}, "incoming": Counter(), "empty": [item]}
            plan = c.plan_changes(root, scanned, {str(path): {"status": "error", "error": "offline"}}, {})
            self.assertFalse(plan["removed"])
            self.assertEqual(plan["work_actions"][0]["action"], "deferred")
            plan = c.plan_changes(root, scanned, {str(path): {"status": "not_found"}}, {})
            self.assertEqual(plan["removed"], {path})

    def test_enrichment_keeps_user_content_and_adds_vndb_facts(self):
        item = note("Stub", body="A personal note.\n")
        row = {"id": "v1", "title": "Original", "released": "2017-06-01", "rating": 72,
               "description": "[b]Synopsis[/b]", "developers": [], "staff": [], "tags": [], "relations": []}
        indices = {k: {} for k in ("developers", "staff", "tags", "works")}
        enriched = c.parse_note(item.path, c.enrich(item, row, indices))
        self.assertEqual(enriched.data["ratings"]["vndb"], 7.2)
        self.assertIn("A personal note.", enriched.body)
        self.assertIn("**Synopsis**", enriched.body)

    def test_staging_does_not_commit_existing_user_edit(self):
        import tempfile
        import subprocess
        import json
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            def git(*args):
                return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL).decode("utf-8")
            git("init", "-q")
            git("config", "user.email", "fixture@example.invalid")
            git("config", "user.name", "Fixture")
            (root / "作品").mkdir()
            path = root / "作品/A.md"
            path.write_text("# A\n[[Old]]\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-qm", "fixture")
            path.write_text("# A\n[[发行版/Keep]]\nUser text.\n", encoding="utf-8")
            directory = root / "系统/内容清理"
            directory.mkdir(parents=True)
            (directory / "manifest.json").write_text(json.dumps({"initial_dirty": ["作品/A.md"],
                                                                 "updated": ["作品/A.md"], "removed": []}), encoding="utf-8")
            (directory / "report.json").write_text(json.dumps({"release_groups": [
                {"keeper": "发行版/Keep.md", "removed": ["发行版/Old.md"]}],
                "work_actions": [], "colliding_basenames": []}), encoding="utf-8")
            (directory / "vndb-empty-work-records.json").write_text("{}", encoding="utf-8")
            c.stage_task_changes(root)
            self.assertEqual(git("show", ":作品/A.md"), "# A\n[[发行版/Keep]]\n")
            self.assertIn("User text.", git("diff", "--", "作品/A.md"))


if __name__ == "__main__":
    unittest.main()
