import tempfile
import unittest
from pathlib import Path

from content_store import ContentStore


class ContentStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ContentStore(Path(self.directory.name) / 'content.sqlite3')

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def _model(self, fingerprint, status='learned', rules=None):
        return {'site': 's', 'kind': 'detail', 'rule_version': 'rules-1.0.0',
                'status': status, 'page_count': 3, 'fingerprint': fingerprint,
                'rules': rules or [{'path': '/nav[1]', 'text': '导航文本',
                                    'document_frequency': 3}]}

    def test_model_versions_are_append_only(self):
        version, created = self.store.save_model(self._model('fp-1'), 't1')
        self.assertEqual((version, created), (1, True))
        version, created = self.store.save_model(self._model('fp-1'), 't2')
        self.assertEqual((version, created), (1, False))
        version, created = self.store.save_model(self._model('fp-2'), 't3')
        self.assertEqual((version, created), (2, True))
        history = self.store.model_history('s', 'detail')
        self.assertEqual([row['fingerprint'] for row in history], ['fp-1', 'fp-2'])
        self.assertEqual(self.store.latest_models()[('s', 'detail')]['fingerprint'], 'fp-2')

    def test_constraints_replace_keeps_canonical_pairs(self):
        self.store.replace_constraints(
            {'confirmed_same': [['b', 'a']], 'confirmed_different': [['a', 'c']]},
            'rules-1.0.0', 't1')
        loaded = self.store.load_constraints()
        self.assertEqual(loaded['confirmed_same'], [['a', 'b']])
        self.assertEqual(loaded['confirmed_different'], [['a', 'c']])
        self.store.replace_constraints({'confirmed_same': [], 'confirmed_different': []},
                                       'rules-1.0.0', 't2')
        self.assertEqual(self.store.load_constraints(),
                         {'confirmed_same': [], 'confirmed_different': []})

    def test_content_version_swap_updates_index_together(self):
        from near_duplicate import build_document
        model = self._model('fp-1')
        blocks = [{'path': '/main[1]/p[1]', 'text': '第一篇足够长的市场简讯正文内容。',
                   'ordinal': 0}]
        document = build_document('art-1', blocks)
        version_one = self.store.save_content_version(
            'art-1', 'digest-1', 's', 'detail', model, blocks, [], document, 't1')
        self.assertEqual(self.store.candidate_pairs_from_index(), [])
        blocks_two = [{'path': '/main[1]/p[1]', 'text': '第一篇足够长的市场简讯正文内容。',
                       'ordinal': 0}]
        document_two = build_document('art-2', blocks_two)
        self.store.save_content_version(
            'art-2', 'digest-2', 's', 'detail', model, blocks_two, [], document_two, 't2')
        self.assertEqual(self.store.candidate_pairs_from_index(), [('art-1', 'art-2')])

        # 新版本切换：旧索引被同事务替换，当前指针指向新版本，旧版本仍可查
        new_blocks = [{'path': '/main[1]/p[1]', 'text': '完全不同的港口运费理赔报道内容。',
                       'ordinal': 0}]
        new_document = build_document('art-1', new_blocks)
        self.store.save_content_version(
            'art-1', 'digest-1b', 's', 'detail', model, new_blocks, [], new_document, 't3')
        self.assertEqual(self.store.candidate_pairs_from_index(), [])
        current = self.store.current_articles()['art-1']
        self.assertEqual(current['version_id'], version_one + 2)
        self.assertEqual(len(self.store.content_versions('art-1')), 2)

    def test_stale_detection_uses_latest_fingerprint(self):
        from near_duplicate import build_document
        model_one = self._model('fp-1')
        blocks = [{'path': '/main[1]/p[1]', 'text': '第一篇足够长的市场简讯正文内容。',
                   'ordinal': 0}]
        self.store.save_content_version(
            'art-1', 'digest-1', 's', 'detail', model_one, blocks, [],
            build_document('art-1', blocks), 't1')
        self.assertEqual(self.store.stale_articles({('s', 'detail'): model_one}), [])
        model_two = self._model('fp-2')
        self.assertEqual(self.store.stale_articles({('s', 'detail'): model_two}), ['art-1'])
        # 尚无模板的站点也视为待处理
        self.assertEqual(self.store.stale_articles({}), ['art-1'])

    def test_runs_groups_and_evidence_round_trip(self):
        run_id = self.store.start_run(
            'batch', 0.55, {'candidate_pairs': 1}, 'rules-1.0.0', 't1')
        articles = {
            'a': {'version_id': 1}, 'b': {'version_id': 2},
        }
        edges = {('a', 'b'): {
            'relation': 'same', 'score': 0.8, 'threshold': 0.55,
            'shared_fragments': ['共同片段'], 'only_a': [], 'only_b': ['乙独有'],
            'reasons': ['similarity_threshold'], 'rule_version': 'rules-1.0.0'}}
        self.store.save_evidence(run_id, articles, edges)
        groups = [{'group_id': 'grp_x', 'status': 'unique', 'representative': 'a',
                   'selection_rule': 'max_content_length_then_min_article_id',
                   'members': ['a', 'b'], 'missing_direct_edges': []}]
        self.store.save_groups(run_id, groups)
        self.assertEqual(self.store.group_of('b')['group_id'], 'grp_x')
        evidence = self.store.evidence('b', 'a')[0]
        self.assertEqual(evidence['article_a'], 'a')
        self.assertEqual(evidence['shared_fragments'], ['共同片段'])
        self.assertEqual(self.store.runs()[0]['stats'], {'candidate_pairs': 1})


if __name__ == '__main__':
    unittest.main()
