import unittest
from pathlib import Path
from text_source import extract


class TextTests(unittest.TestCase):
    def test_hidden_and_script_ignored(self):
        rows=extract('<div>正文</div><script>秘密</script><p hidden>隐文</p>')
        self.assertEqual([row['text'] for row in rows],['正文'])

    def test_entities(self):
        self.assertEqual(extract('<p>甲 &amp; 乙</p>')[0]['text'],'甲 & 乙')

    def test_repeated_sibling_paths(self):
        rows=extract('<main><p>甲</p><p>乙</p></main>')
        self.assertNotEqual(rows[0]['path'],rows[1]['path'])
        self.assertEqual(rows[1]['ordinal'],1)

    def test_raw_template_is_present(self):
        rows=extract(Path('fixtures/corpus/north-short.html').read_text())
        self.assertTrue(any('服务声明' in row['text'] for row in rows))
        self.assertTrue(any(row['text']=='电机登记已结束。' for row in rows))

    def test_void_nodes_do_not_change_path(self):
        rows=extract('<main><img src="local"><p>正文</p></main>')
        self.assertEqual(rows[0]['path'],'/main[1]/p[1]')
