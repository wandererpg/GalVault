from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deduplicate_exact_releases as dedup


def release_page(release_id: str, filename: str) -> str:
    return f'''---
entity_id: "release:vndb:{release_id}:work:vndb:work:v123"
type: "release"
display_title: "Sample Release"
release_date: "2020-01-01"
is_all_ages: false
platform: "win"
edition_type: "complete"
source_ids:
  vndb: "{release_id}"
source_urls:
  vndb: "https://vndb.org/{release_id}"
---

# Sample Release
<!-- AUTO:BEGIN release -->- [[Sample Work]]<!-- AUTO:END release -->

## 来源
<!-- AUTO:BEGIN provenance -->vndb:{release_id}<!-- AUTO:END provenance -->
'''


class ExactReleaseDedupTests(unittest.TestCase):
    def test_normalization_only_ignores_release_ids(self) -> None:
        left = release_page("r10", "left.md")
        right = release_page("r20", "right.md")
        self.assertEqual(dedup.normalized_release_text(left), dedup.normalized_release_text(right))
        changed = right.replace('release_date: "2020-01-01"', 'release_date: "2021-01-01"')
        self.assertNotEqual(dedup.normalized_release_text(left), dedup.normalized_release_text(changed))

    def test_detects_only_exact_duplicates_within_same_work(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            releases = root / "发行版"
            releases.mkdir()
            (releases / "Sample Release.md").write_text(release_page("r10", "a"), encoding="utf-8")
            (releases / "Sample Release (source).md").write_text(release_page("r20", "b"), encoding="utf-8")
            other_work = release_page("r30", "c").replace("work:vndb:work:v123", "work:vndb:work:v456")
            (releases / "Other Work.md").write_text(other_work, encoding="utf-8")
            self.assertEqual(len(dedup.find_exact_duplicate_groups(root)), 1)

    def test_merge_repoints_links_deduplicates_auto_lists_and_preserves_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            releases = root / "发行版"
            works = root / "作品"
            releases.mkdir()
            works.mkdir()
            keeper = releases / "Sample Release.md"
            duplicate = releases / "Sample Release (source).md"
            keeper.write_text(release_page("r10", "a"), encoding="utf-8")
            duplicate.write_text(release_page("r20", "b"), encoding="utf-8")
            work = works / "Sample Work.md"
            work.write_text(
                "---\ntype: work\n---\n"
                "<!-- AUTO:BEGIN release -->- [[Sample Release]]\n"
                "- [[Sample Release (source)]]<!-- AUTO:END release -->\n"
                "See [[Sample Release (source)#来源|source]].\n",
                encoding="utf-8",
            )
            company = root / "Publisher.md"
            company.write_text("- [[Sample Release]]\n", encoding="utf-8")

            plans = dedup.make_merge_plans(root)
            self.assertEqual(len(plans), 1)
            self.assertEqual(plans[0].keeper.path, keeper)
            dedup.apply_merge_plans(root, plans)

            self.assertTrue(keeper.exists())
            self.assertFalse(duplicate.exists())
            merged = keeper.read_text(encoding="utf-8")
            self.assertIn('id: "r10"', merged)
            self.assertIn('id: "r20"', merged)
            self.assertIn('"Sample Release (source)"', merged)
            updated_work = work.read_text(encoding="utf-8")
            self.assertEqual(updated_work.count("[[Sample Release]]"), 1)
            self.assertIn("[[Sample Release#来源|source]]", updated_work)
            self.assertNotIn("[[Sample Release (source)", updated_work)


if __name__ == "__main__":
    unittest.main()
