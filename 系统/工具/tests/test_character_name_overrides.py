from __future__ import annotations

import csv
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
EXPECTED = {
    "c44134": ("Echo.md", "艾可"),
    "c104868": ("Aster.md", "艾斯特"),
    "c138186": ("Artemis.md", "阿尔忒弥斯"),
    "c34311": ("Lune.md", "琉恩"),
    "c165386": ("Samantha.md", "萨曼莎"),
    "c161299": ("Snow.md", "思诺"),
}


class CharacterNameOverrideTests(unittest.TestCase):
    def test_override_csv_matches_character_page_titles(self) -> None:
        csv_path = ROOT / "系统" / "角色中文化" / "中文名修订.csv"
        with csv_path.open(encoding="utf-8-sig", newline="") as source:
            rows = {row["entity_id"].rsplit(":", 1)[-1]: row for row in csv.DictReader(source)}

        for character_id, (filename, expected_title) in EXPECTED.items():
            with self.subTest(character=character_id):
                self.assertEqual(rows[character_id]["title_zh"], expected_title)
                self.assertTrue(rows[character_id]["notes"].strip())
                text = (ROOT / "角色" / filename).read_text(encoding="utf-8")
                self.assertRegex(text, rf'(?m)^display_title: "{re.escape(expected_title)}"$')
                self.assertRegex(text, rf'(?m)^title_zh: "{re.escape(expected_title)}"$')
                self.assertRegex(text, r'(?m)^title_zh_quality: "manual_override"$')
                self.assertRegex(text, rf'(?m)^# {re.escape(expected_title)}$')


if __name__ == "__main__":
    unittest.main()
