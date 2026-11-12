import unittest

from near_duplicate import (
    DEFAULT_MERGE_THRESHOLD,
    ShingleIndex,
    build_document,
    build_groups,
    canonical_pair,
    compare_documents,
    resolve_constraints,
    score_edges,
    split_fragments,
)


def doc(article_id, text):
    return build_document(article_id, [{'path': '/main[1]/article[1]/p[1]',
                                        'text': text, 'ordinal': 0}])


class FragmentTests(unittest.TestCase):
    def test_split_ignores_punctuation_and_keeps_order(self):
        fragments = split_fragments('合成市场甲发布说明。登记窗口，十一月开始！')
        self.assertEqual(fragments, ['合成市场甲发布说明', '登记窗口', '十一月开始'])

    def test_shingles_are_deterministic_sets(self):
        left = doc('a', '合成市场甲发布新的电机准入说明。登记窗口从十一月开始。')
        right = doc('a', '合成市场甲发布新的电机准入说明。登记窗口从十一月开始。')
        self.assertEqual(left['shingles'], right['shingles'])
        self.assertGreater(len(left['shingles']), 1)

    def test_evidence_lists_shared_and_unique_fragments(self):
        left = doc('a', '合成项目甲启动。共同消息一。共同消息二。专属消息甲。')
        right = doc('b', '合成项目甲启动。共同消息一。共同消息二。合成项目乙结束。')
        evidence = compare_documents(left, right)
        self.assertEqual(evidence['article_a'], 'a')
        self.assertEqual(evidence['article_b'], 'b')
        self.assertIn('共同消息一', evidence['shared_fragments'])
        self.assertIn('专属消息甲', evidence['only_a'])
        self.assertIn('合成项目乙结束', evidence['only_b'])
        self.assertTrue(0 < evidence['score'] < 1)

    def test_heading_change_does_not_change_body_similarity(self):
        from near_duplicate import build_document as build
        left = build('a', [
            {'path': '/main[1]/h1[1]', 'text': '原站首发标题', 'ordinal': 0},
            {'path': '/main[1]/article[1]/p[1]',
             'text': '合成市场甲发布新的电机准入说明。登记窗口从十一月开始。', 'ordinal': 1}])
        right = build('b', [
            {'path': '/main[1]/h1[1]', 'text': '转载站换了一个完全不同的吸引眼球标题', 'ordinal': 0},
            {'path': '/main[1]/article[1]/p[1]',
             'text': '合成市场甲发布新的电机准入说明。登记窗口从十一月开始。', 'ordinal': 1}])
        evidence = compare_documents(left, right)
        self.assertEqual(evidence['score'], 1.0)
        self.assertEqual(evidence['only_a'], [])
        self.assertEqual(evidence['only_b'], [])


class IndexTests(unittest.TestCase):
    def test_candidates_only_come_from_shared_shingles(self):
        index = ShingleIndex()
        index.add(doc('a', '合成市场甲发布新的电机准入说明。登记窗口从十一月开始。'))
        index.add(doc('b', '完全无关的港口物流价格波动报道内容摘要。'))
        index.add(doc('c', '合成市场甲发布新的电机准入说明。旧证书可用到年底。'))
        pairs = index.candidate_pairs()
        self.assertIn(('a', 'c'), pairs)
        self.assertNotIn(('a', 'b'), pairs)
        self.assertNotIn(('b', 'c'), pairs)

    def test_remove_and_re_add_keeps_index_consistent(self):
        index = ShingleIndex()
        index.add(doc('a', '合成市场甲发布新的电机准入说明。'))
        index.add(doc('b', '合成市场甲发布新的电机准入说明。'))
        self.assertEqual(index.candidate_pairs(), [('a', 'b')])
        index.remove('a')
        self.assertEqual(index.candidate_pairs(), [])
        index.add(doc('a', '完全不同的港口集装箱运费调整消息内容。'))
        self.assertEqual(index.candidate_pairs(), [])


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.constraints = {'threshold': DEFAULT_MERGE_THRESHOLD,
                            'confirmed_same': [], 'confirmed_different': []}

    def _edges(self, documents):
        index = ShingleIndex()
        for document in documents.values():
            index.add(document)
        edges, _stats = score_edges(documents, index, DEFAULT_MERGE_THRESHOLD, self.constraints)
        return edges

    def test_clique_becomes_unique_group_with_deterministic_representative(self):
        text = '合成市场甲发布新的电机准入说明。登记窗口从十一月开始，旧证书可用到年底。'
        documents = {
            'north-1': doc('north-1', text),
            'north-3': doc('north-3', text),
            'south-copy': doc('south-copy', text),
        }
        groups = build_groups(documents, self._edges(documents))
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group['status'], 'unique')
        self.assertEqual(group['members'], ['north-1', 'north-3', 'south-copy'])
        # 同长度时取 id 字典序最小，与入库顺序无关
        self.assertEqual(group['representative'], 'north-1')
        self.assertEqual(group['missing_direct_edges'], [])

    def test_representative_prefers_longest_content(self):
        text = '合成市场甲发布新的电机准入说明。登记窗口从十一月开始，旧证书可用到年底。'
        documents = {
            'short': doc('short', text),
            'long': doc('long', text + '企业还应提前准备授权材料并在窗口关闭前完成现场核验。'),
        }
        group = build_groups(documents, self._edges(documents))[0]
        self.assertEqual(group['representative'], 'long')
        self.assertEqual(group['selection_rule'], 'max_content_length_then_min_article_id')

    def test_similarity_chain_is_not_transitively_merged(self):
        documents = {
            'chain-a': doc('chain-a', '合成项目甲启动。共同消息一。共同消息二。专属消息甲。'),
            'chain-b': doc('chain-b', '合成项目甲启动。共同消息一。共同消息二。合成项目乙结束。共同消息三。共同消息四。'),
            'chain-c': doc('chain-c', '合成项目乙结束。共同消息三。共同消息四。专属消息乙。'),
        }
        edges = self._edges(documents)
        # 链上相邻对有证据但未达阈值，a-c 更弱；任何同稿边都不允许靠传递产生
        self.assertTrue(all(edge['relation'] == 'different' for edge in edges.values()))
        groups = build_groups(documents, edges)
        self.assertEqual(sorted(group['members'] for group in groups),
                         [['chain-a'], ['chain-b'], ['chain-c']])

    def test_non_clique_component_is_kept_ambiguous(self):
        # 手工构造 a≈b、b≈c 达阈值但 a≈c 不达标：禁止取闭包，保留歧义
        shared_ab = ''.join(f'甲篇共享叙述内容编号{i:02d}。' for i in range(12))
        shared_bc = ''.join(f'乙篇共享叙述内容编号{i:02d}。' for i in range(12))
        documents = {
            'a': doc('a', shared_ab + '甲篇独有的补充说明段落。'),
            'b': doc('b', shared_ab + shared_bc),
            'c': doc('c', shared_bc + '丙篇独有结尾说明。'),
        }
        edges = self._edges(documents)
        relations = {pair: edge['relation'] for pair, edge in edges.items()}
        self.assertEqual(relations[('a', 'b')], 'same')
        self.assertEqual(relations[('b', 'c')], 'same')
        self.assertEqual(relations[('a', 'c')], 'different')
        groups = build_groups(documents, edges)
        self.assertEqual(len(groups), 1)
        ambiguous = groups[0]
        self.assertEqual(ambiguous['status'], 'ambiguous')
        self.assertEqual(ambiguous['members'], ['a', 'b', 'c'])
        self.assertEqual(ambiguous['missing_direct_edges'], [('a', 'c')])
        # 歧义组仍要给出确定性代表稿，不碰巧取第一个
        self.assertIn(ambiguous['representative'], ('a', 'b', 'c'))
        self.assertEqual(ambiguous['representative'],
                         build_groups(documents, edges)[0]['representative'])

    def test_cannot_link_blocks_threshold_merge(self):
        text = '合成市场甲发布新的电机准入说明。登记窗口从十一月开始，旧证书可用到年底。'
        documents = {'x': doc('x', text), 'y': doc('y', text)}
        constraints = {'threshold': DEFAULT_MERGE_THRESHOLD,
                       'confirmed_same': [], 'confirmed_different': [['x', 'y']]}
        index = ShingleIndex()
        index.add(documents['x'])
        index.add(documents['y'])
        edges, stats = score_edges(documents, index, DEFAULT_MERGE_THRESHOLD, constraints)
        self.assertEqual(edges[('x', 'y')]['relation'], 'blocked')
        self.assertEqual(stats['blocked_edges'], 1)
        groups = build_groups(documents, edges)
        self.assertEqual(sorted(group['members'] for group in groups), [['x'], ['y']])

    def test_must_link_forces_edge_with_direct_evidence(self):
        documents = {'x': doc('x', '第一篇讲电机登记安排的具体细节内容。'),
                     'y': doc('y', '第二篇讲食品标签咨询会议的不同内容。')}
        constraints = {'threshold': DEFAULT_MERGE_THRESHOLD,
                       'confirmed_same': [['y', 'x']], 'confirmed_different': []}
        index = ShingleIndex()
        for document in documents.values():
            index.add(document)
        edges, _stats = score_edges(documents, index, DEFAULT_MERGE_THRESHOLD, constraints)
        self.assertEqual(edges[canonical_pair('x', 'y')]['relation'], 'same')
        self.assertIn('editor_confirmed_same', edges[('x', 'y')]['reasons'])

    def test_contradictory_constraints_are_rejected(self):
        with self.assertRaises(ValueError):
            resolve_constraints({'confirmed_same': [['x', 'y']],
                                 'confirmed_different': [['y', 'x']]})

    def test_grouping_is_order_independent(self):
        text = '合成市场甲发布新的电机准入说明。登记窗口从十一月开始，旧证书可用到年底。'
        documents = {name: doc(name, text) for name in ('z', 'a', 'm')}
        documents['loner'] = doc('loner', '毫不相关的港口运费保险理赔流程报道内容。')
        first = build_groups(documents, self._edges(documents))
        reordered = dict(reversed(list(documents.items())))
        second = build_groups(reordered, self._edges(reordered))
        self.assertEqual([group['group_id'] for group in first],
                         [group['group_id'] for group in second])
        self.assertEqual([group['representative'] for group in first],
                         [group['representative'] for group in second])
