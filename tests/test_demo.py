import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DemoTests(unittest.TestCase):
    def test_real_demo(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'capture.sqlite3'
            process=subprocess.run([sys.executable,'app.py','demo','--output',str(output)],capture_output=True,text=True,timeout=30)
            self.assertEqual(process.returncode,0,process.stderr)
            payload=json.loads(process.stdout)
            self.assertIsInstance(payload,dict)
            self.assertTrue(output.is_file())
            self.assertTrue(payload['批量与增量一致'])
            self.assertLess(payload['候选比较报告']['实际打分候选对'],
                            payload['候选比较报告']['全量逐对比较上限'])
            # 转载同稿组：north-1/north-3/south-copy
            groups={tuple(group['成员']): group for group in payload['内容组']}
            merged=groups[('north-1','north-3','south-copy')]
            self.assertEqual(merged['状态'],'unique')
            self.assertEqual(merged['代表稿'],'north-1')
            # 相似链三篇各自独立，未被链式合并
            for article_id in ('chain-a','chain-b','chain-c'):
                self.assertIn((article_id,), groups)

    def test_query_evidence_and_group(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'capture.sqlite3'
            subprocess.run([sys.executable,'app.py','demo','--output',str(output)],
                           capture_output=True,text=True,timeout=30,check=True)
            pair=subprocess.run([sys.executable,'app.py','query','--output',str(output),
                                 '--pair','north-1,south-copy'],
                                capture_output=True,text=True,timeout=30)
            self.assertEqual(pair.returncode,0,pair.stderr)
            evidence=json.loads(pair.stdout)['差异证据'][0]
            self.assertEqual(evidence['relation'],'same')
            self.assertTrue(evidence['shared_fragments'])
            article=subprocess.run([sys.executable,'app.py','query','--output',str(output),
                                    '--article','chain-a'],
                                   capture_output=True,text=True,timeout=30)
            self.assertEqual(article.returncode,0,article.stderr)
            payload=json.loads(article.stdout)
            self.assertEqual(payload['文章组']['members'],['chain-a'])
