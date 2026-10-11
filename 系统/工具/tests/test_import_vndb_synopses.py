import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

SPEC = importlib.util.spec_from_file_location(
    "synopses", Path(__file__).resolve().parents[1] / "import_vndb_synopses.py")
s = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(s)
NOTE = '---\nentity_id: "vndb:work:v123"\n---\n# 标题\n\n## 封面\n\n![[封面.jpg]]\n\n## 关系\n\n[[角色]]\n'
RECORD = {"id": "v123", "title": "Title", "description": "A story."}


class SynopsisTests(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(s.vndb_id(NOTE), "v123")
        self.assertEqual(s.vndb_id('---\nsource_ids:\n  vndb: "v45"\n---\n'), "v45")
        self.assertEqual(s.vndb_id('---\nsource_urls:\n  vndb: https://vndb.org/v67\n---\n'), "v67")
        self.assertIsNone(s.vndb_id('# 标题\nhttps://vndb.org/v123'))

    def test_position_and_preservation(self):
        result = s.add_synopsis(NOTE, RECORD)
        self.assertTrue(result.startswith(NOTE.split('## 关系')[0]))
        self.assertTrue(result.endswith('## 关系\n\n[[角色]]\n'))
        self.assertLess(result.index('## 封面'), result.index('## 简介'))
        self.assertLess(result.index('## 简介'), result.index('## 关系'))
        self.assertIn('https://vndb.org/v123', result)

    def test_manual_intro_never_overwritten(self):
        for title in ('简介', '简介（VNDB 原文）', '剧情介绍', 'Synopsis'):
            note = NOTE + f'\n## {title}\n\n人工简介\n'
            self.assertEqual(s.add_synopsis(note, RECORD, True), note)
        note = NOTE + '\n## 简介\n待补充\n## 剧情简介\n人工简介\n'
        self.assertEqual(s.add_synopsis(note, RECORD, True), note)

    def test_placeholder(self):
        note = NOTE.replace('## 关系', '## 简介\n\n待补充\n\n## 关系')
        result = s.add_synopsis(note, RECORD)
        self.assertEqual(result.count('## 简介'), 1)
        self.assertNotIn('待补充', result)
        self.assertIn('[[角色]]', result)

    def test_idempotence_and_refresh(self):
        first = s.add_synopsis(NOTE, RECORD)
        changed = dict(RECORD, description='Changed.')
        self.assertEqual(s.add_synopsis(first, changed), first)
        self.assertIn('Changed.', s.add_synopsis(first, changed, True))
        self.assertEqual(s.add_synopsis(first, dict(RECORD, description=None), True), first)
        manual = first.replace(s.END, s.END + '\n人工添加')
        self.assertEqual(s.add_synopsis(manual, changed, True), manual)

    def test_bom_crlf(self):
        note = '\ufeff' + NOTE.replace('\n', '\r\n')
        result = s.add_synopsis(note, RECORD)
        self.assertTrue(result.startswith('\ufeff---\r\n'))
        self.assertNotIn('\n', result.replace('\r\n', ''))
        placeholder = note.replace('## 关系', '## 简介\r\n\r\n待补充\r\n\r\n## 关系')
        self.assertNotIn('\r\r\n', s.add_synopsis(placeholder, RECORD))

    def test_bbcode(self):
        result = s.markdown_description('[b]Bold[/b]  \r\n[url=https://example.com]Link[/url]\n' + s.END)
        self.assertIn('**Bold**\n[Link](https://example.com)', result)
        self.assertNotIn(s.END, result)

    def setup_vault(self, base):
        root = Path(base) / 'vault'
        (root / '作品').mkdir(parents=True)
        (root / '作品/test.md').write_bytes(NOTE.encode('utf-8'))
        return root

    def seed_cache(self, root, value=None):
        s.save_json(root / '系统/简介导入/cache.json', {
            'v123': value or {'status': 'found', 'record': RECORD}})

    def test_default_preview_no_writes_or_network(self):
        with tempfile.TemporaryDirectory() as base:
            root = self.setup_vault(base)
            before = sorted(str(p) for p in root.rglob('*'))
            with patch.object(s, 'api_fetch') as api:
                report = s.import_synopses(root)
            api.assert_not_called()
            self.assertEqual(report['summary'], {'needs_fetch': 1})
            self.assertEqual(before, sorted(str(p) for p in root.rglob('*')))

    def test_previous_cache_reused(self):
        with tempfile.TemporaryDirectory() as base:
            root = self.setup_vault(base)
            s.save_json(root / '系统/内容清理/vndb-empty-work-records.json', {
                'v123': {'status': 'found', 'record': RECORD}})
            with patch.object(s, 'api_fetch') as api:
                report = s.import_synopses(root)
            api.assert_not_called()
            self.assertEqual(report['summary'], {'would_add': 1})
            self.assertFalse((root / '系统/简介导入').exists())

    def test_apply_backup_and_second_run(self):
        with tempfile.TemporaryDirectory() as base:
            root = self.setup_vault(base)
            self.seed_cache(root)
            with patch.object(s, 'api_fetch') as api:
                report = s.import_synopses(root, apply=True)
                again = s.import_synopses(root, apply=True)
            api.assert_not_called()
            self.assertEqual(report['summary'], {'added': 1})
            self.assertEqual(again['summary'], {'existing_intro': 1})
            with zipfile.ZipFile(report['backup']) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(archive.read('作品/test.md'), NOTE.encode('utf-8'))

    def test_fetch_only(self):
        with tempfile.TemporaryDirectory() as base:
            root = self.setup_vault(base)
            with patch.object(s, 'api_fetch', return_value=[RECORD]):
                report = s.import_synopses(root, fetch=True)
            self.assertEqual(report['summary'], {'would_add': 1})
            self.assertEqual((root / '作品/test.md').read_bytes(), NOTE.encode('utf-8'))
            self.assertTrue((root / '系统/简介导入/cache.json').exists())

    def test_unavailable_records_retained(self):
        for response, status in (([], 'not_found'), ([dict(RECORD, description=None)], 'no_description')):
            with tempfile.TemporaryDirectory() as base:
                root = self.setup_vault(base)
                with patch.object(s, 'api_fetch', return_value=response):
                    report = s.import_synopses(root, apply=True)
                self.assertEqual(report['summary'], {status: 1})
                self.assertIsNone(report['backup'])
                self.assertEqual((root / '作品/test.md').read_bytes(), NOTE.encode('utf-8'))

    def test_refresh_failure_preserves_old_block(self):
        with tempfile.TemporaryDirectory() as base:
            root = self.setup_vault(base)
            self.seed_cache(root)
            original = s.add_synopsis(NOTE, RECORD).encode('utf-8')
            (root / '作品/test.md').write_bytes(original)
            with patch.object(s, 'api_fetch', side_effect=RuntimeError('offline')):
                report = s.import_synopses(root, apply=True, refresh=True)
            self.assertEqual(report['summary'], {'request_failed': 1})
            self.assertEqual((root / '作品/test.md').read_bytes(), original)
            self.assertEqual(s.load_cache(root)['v123']['record'], RECORD)

    def test_api_payload_and_validation(self):
        response = io.BytesIO(json.dumps({'results': [RECORD], 'more': False}).encode())
        with patch.object(s.urllib.request, 'urlopen', return_value=response) as urlopen:
            self.assertEqual(s.api_fetch(['v123']), [RECORD])
        payload = json.loads(urlopen.call_args.args[0].data)
        self.assertEqual(payload['filters'], ['id', '=', 'v123'])
        self.assertEqual(payload['fields'], 'id,title,description')
        for data in ({'results': None}, {'results': [{'id': 'v999'}]}, {'results': [dict(RECORD, description=42)]}):
            with patch.object(s.urllib.request, 'urlopen', return_value=io.BytesIO(json.dumps(data).encode())):
                with self.assertRaises(ValueError):
                    s.api_fetch(['v123'])

    def test_api_retries(self):
        with patch.object(s.urllib.request, 'urlopen', side_effect=urllib.error.URLError('offline')) as request:
            with patch.object(s.time, 'sleep'):
                with self.assertRaises(RuntimeError):
                    s.api_fetch(['v123'], retries=2)
        self.assertEqual(request.call_count, 2)


if __name__ == '__main__':
    unittest.main()
