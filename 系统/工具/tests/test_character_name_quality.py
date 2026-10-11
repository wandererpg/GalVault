import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('name_quality', Path(__file__).resolve().parents[1] / 'localize_character_names.py')
n = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(n)


class NameQualityTests(unittest.TestCase):
    def test_kana_never_guesses_kanji(self):
        self.assertEqual(n.generated_title({'original': 'さくら', 'name': 'Sakura'}), ('さくら', 'original_pending_review'))

    def test_mixed_name_retained(self):
        self.assertEqual(n.generated_title({'original': '鳴瀬 しろは', 'name': 'Naruse Shiroha'}), ('鳴瀬 しろは', 'original_pending_review'))

    def test_roman_retained(self):
        self.assertEqual(n.generated_title({'name': 'Samantha'}), ('Samantha', 'original_pending_review'))

    def test_han_and_role(self):
        self.assertEqual(n.generated_title({'original': '古河 渚', 'name': 'Furukawa Nagisa'}), ('古河渚', 'converted_original'))
        self.assertEqual(n.generated_title({'name': 'Teacher'}), ('老师', 'glossary'))

    def test_manual_title_preserved(self):
        item = {'original': '鳴瀬 しろは', 'name': 'Naruse Shiroha'}
        note = '---\ndisplay_title: 鸣濑白羽\ntitle_zh_quality: converted_original\n---\n# 鸣濑白羽\n'
        self.assertEqual(n.select_title(note, item, None), ('鸣濑白羽', 'manual_local'))
        self.assertEqual(n.select_title(note, item, '人工修订'), ('人工修订', 'manual_override'))

    def test_pending_metadata_and_alias_preserved(self):
        note = '---\ndisplay_title: "旧译"\naliases:\n  - "手写别名"\nsource_ids:\n  vndb: c1\n---\n# 旧译\n\n## 关系\n[[作品]]\n'
        item = {'original': 'さくら', 'name': 'Sakura'}
        result = n.replace_frontmatter(note, 'c1', item, 'さくら', 'original_pending_review', 'https://vndb.org/c1')
        self.assertIn('title_zh: ""', result)
        self.assertIn('title_status: "needs_review"', result)
        self.assertIn('title_zh_confidence: "low"', result)
        self.assertIn('手写别名', result)
        self.assertTrue(result.endswith('## 关系\n[[作品]]\n'))
        self.assertEqual(result, n.replace_frontmatter(result, 'c1', item, 'さくら', 'original_pending_review', 'https://vndb.org/c1'))

    def test_unquoted_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'x.md'
            path.write_text('---\nsource_ids:\n  vndb: c123\n---\n', encoding='utf-8')
            with patch.object(n, 'CHARACTER_DIR', Path(tmp)):
                self.assertEqual(n.character_files(), [(path, 'c123')])

    def test_apply_backup_and_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'vault'
            work = root / '系统/角色中文化'
            (root / '角色').mkdir(parents=True)
            page = root / '角色/x.md'
            raw = '---\ndisplay_title: "樱"\ntitle_zh_quality: "phonetic_transliteration"\n---\n# 樱\n\n[[作品]]\n'.encode('utf-8')
            page.write_bytes(raw)
            cache = {'c1': {'name': 'Sakura', 'original': 'さくら'}}
            with patch.multiple(n, ROOT=root, WORK_DIR=work, OVERRIDE_PATH=work/'中文名修订.csv', REPORT_PATH=work/'生成报告.md'):
                n.apply_titles([(page, 'c1')], cache, dry_run=True)
                self.assertEqual(page.read_bytes(), raw)
                self.assertFalse(work.exists())
                n.apply_titles([(page, 'c1')], cache)
                backups = list((Path(tmp)/'GalVault-maintenance-backups').glob('*.zip'))
                self.assertEqual(len(backups), 1)
                with zipfile.ZipFile(backups[0]) as archive:
                    self.assertEqual(archive.read('角色/x.md'), raw)
                    self.assertIsNone(archive.testzip())
                self.assertIn('title_status: "needs_review"', page.read_text(encoding='utf-8'))
                self.assertTrue((work/'待校对.csv').exists())

    def test_missing_cache_prevents_partial_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = Path(tmp) / 'x.md'
            raw = '---\ndisplay_title: 樱\n---\n# 樱\n'
            page.write_text(raw, encoding='utf-8')
            with patch.object(n, 'ROOT', Path(tmp)):
                with self.assertRaises(RuntimeError):
                    n.apply_titles([(page, 'c999999')], {})
            self.assertEqual(page.read_text(encoding='utf-8'), raw)


if __name__ == '__main__':
    unittest.main()
