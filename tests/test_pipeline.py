import json
import tempfile
import unittest
from pathlib import Path

from content_store import ContentStore
from pipeline import Pipeline
from raw_store import RawStore

FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'


class PipelineTestBase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = Path(self.directory.name) / 'capture.sqlite3'
        self.raw_store = RawStore(self.db_path)
        self.content_store = ContentStore(self.db_path)
        self.truth = json.loads((FIXTURES / 'truth.json').read_text())
        self.entries = json.loads((FIXTURES / 'manifest.json').read_text())
        self.pipeline = Pipeline(self.raw_store, self.content_store,
                                 threshold=self.truth['threshold'])
        self.pipeline.install_constraints(self.truth, '2026-11-12T00:00:00Z')

    def tearDown(self):
        self.content_store.close()
        self.raw_store.close()
        self.directory.cleanup()

    def raw_bytes(self):
        return {entry['id']: (FIXTURES / 'corpus' / Path(entry['path']).name).read_bytes()
                for entry in self.entries}

    def batch_run(self):
        return self.pipeline.ingest(self.entries, 'batch', '2026-11-12T00:00:00Z',
                                    raw_by_id=self.raw_bytes())


class BatchIncrementalConsistencyTests(PipelineTestBase):
    def test_batch_shuffled_and_incremental_converge(self):
        batch = self.batch_run()

        # 乱序、逐篇入库的增量流程
        shuffled = list(self.entries)
        order = [7, 0, 11, 3, 1, 9, 5, 2, 8, 4, 10, 6]
        shuffled = [shuffled[i] for i in order]
        raw = self.raw_bytes()
        runs = []
        for index in range(len(shuffled)):
            chunk = shuffled[:index + 1]
            runs.append(self.pipeline.ingest(chunk, 'incremental', '2026-11-12T00:00:00Z',
                                             raw_by_id={entry['id']: raw[entry['id']] for entry in chunk}))
        incremental = runs[-1]

        # 最终归组、代表稿、组 id 与批量完全一致
        self.assertEqual(
            [(g['group_id'], g['status'], g['representative'], g['members'])
             for g in incremental['groups']],
            [(g['group_id'], g['status'], g['representative'], g['members'])
             for g in batch['groups']])

        # 证据对、分数、关系一致；候选比较数量一致且远小于全量逐对比较
        batch_evidence = {(e['article_a'], e['article_b']): (e['score'], e['relation'])
                          for e in self.content_store.evidence(run_id=batch['run_id'])}
        inc_evidence = {(e['article_a'], e['article_b']): (e['score'], e['relation'])
                        for e in self.content_store.evidence(run_id=incremental['run_id'])}
        self.assertEqual(inc_evidence, batch_evidence)
        self.assertEqual(incremental['stats']['candidate_pairs'],
                         batch['stats']['candidate_pairs'])
        all_pairs = len(self.entries) * (len(self.entries) - 1) // 2
        self.assertLess(batch['stats']['candidate_pairs'], all_pairs)
        self.assertEqual(batch['stats']['scored_pairs'],
                         batch['stats']['candidate_pairs'])
        # 最终候选 = 倒排索引候选 ∪ 编辑约束对 ∪ 相同字节对
        constraint_pairs = (batch['stats']['must_link_pairs'] +
                            batch['stats']['cannot_link_pairs'])
        self.assertGreaterEqual(batch['stats']['index_candidate_pairs'],
                                batch['stats']['candidate_pairs'] - constraint_pairs -
                                batch['stats']['forced_identical_pairs'])

    def test_full_recompute_reproduces_same_result(self):
        first = self.batch_run()
        second = self.pipeline.ingest(self.entries, 'batch', '2026-11-12T00:00:00Z',
                                      raw_by_id=self.raw_bytes())
        self.assertEqual(
            [g['group_id'] for g in first['groups']],
            [g['group_id'] for g in second['groups']])
        self.assertEqual(second['stats']['new_articles'], [])
        # 重新采集不产生重复观察，原文对象不重复
        observations = self.raw_store.observations()
        self.assertEqual(len(observations), len(self.entries))

    def test_partial_site_incremental_keeps_other_models_stable(self):
        self.batch_run()
        raw = self.raw_bytes()
        one = [entry for entry in self.entries if entry['id'] == 'north-1']
        out = self.pipeline.ingest(one, 'incremental', '2026-11-12T00:00:00Z',
                                   raw_by_id={'north-1': raw['north-1']})
        self.assertEqual(out['stats']['model_versions_changed'], [])
        self.assertEqual(out['stats']['reprocessed_articles'], [])
        self.assertEqual(out['stats']['new_articles'], [])
        # 没有新正文版本产生
        self.assertEqual(len(self.content_store.content_versions('north-1')), 1)
        self.assertEqual(len(self.content_store.content_versions('south-copy')), 1)

    def test_intermediate_runs_remain_queryable(self):
        raw = self.raw_bytes()
        first_chunk = self.entries[:3]
        first = self.pipeline.ingest(
            first_chunk, 'incremental', '2026-11-12T00:00:00Z',
            raw_by_id={entry['id']: raw[entry['id']] for entry in first_chunk})
        self.batch_run()
        # 早期运行的证据与组按 run_id 仍可查询，不被后续运行覆盖
        early_groups = self.content_store.groups(run_id=first['run_id'])
        early_members = {member for group in early_groups for member in group['members']}
        self.assertEqual(early_members, {entry['id'] for entry in first_chunk})
        self.assertEqual(len(self.content_store.runs()), 2)

    def test_confirmed_examples_appear_as_evidence(self):
        self.batch_run()
        evidence = {(e['article_a'], e['article_b']): e for e in self.content_store.evidence()}
        same = evidence[('north-1', 'south-copy')]
        self.assertEqual(same['relation'], 'same')
        self.assertIn('editor_confirmed_same', same['reasons'])
        self.assertTrue(same['shared_fragments'])
        different = evidence[('north-1', 'north-2')]
        self.assertEqual(different['relation'], 'different')
        self.assertIn('editor_confirmed_different', different['reasons'])

    def test_similarity_chain_remains_three_separate_articles(self):
        self.batch_run()
        groups = {tuple(g['members']): g for g in self.content_store.groups()}
        chain_ids = {'chain-a', 'chain-b', 'chain-c'}
        for article_id in chain_ids:
            group = self.content_store.group_of(article_id)
            self.assertEqual(group['members'], [article_id])
            self.assertEqual(group['status'], 'unique')
        evidence = self.content_store.evidence()
        chain_edges = [e for e in evidence
                       if e['article_a'].startswith('chain') and e['article_b'].startswith('chain')]
        self.assertEqual(len(chain_edges), 3)
        self.assertTrue(all(e['relation'] == 'different' for e in chain_edges))

    def test_republished_article_group_has_direct_evidence_for_every_pair(self):
        self.batch_run()
        group = self.content_store.group_of('north-3')
        self.assertEqual(group['members'], ['north-1', 'north-3', 'south-copy'])
        self.assertEqual(group['representative'], 'north-1')
        pair_ids = {('north-1', 'north-3'), ('north-1', 'south-copy'), ('north-3', 'south-copy')}
        evidence_pairs = {(e['article_a'], e['article_b']): e for e in self.content_store.evidence()}
        self.assertTrue(pair_ids <= set(evidence_pairs))
        for pair in pair_ids:
            self.assertEqual(evidence_pairs[pair]['relation'], 'same')
            self.assertGreaterEqual(evidence_pairs[pair]['score'], self.truth['threshold'])


class QueryAndPreservationTests(PipelineTestBase):
    def test_groups_and_evidence_are_queryable_with_fragments(self):
        self.batch_run()
        groups = self.content_store.groups()
        self.assertEqual(len(groups), len(self.entries) - 2)  # 三篇同稿合并为一个组
        evidence = self.content_store.evidence('north-1', 'south-copy')
        self.assertEqual(len(evidence), 1)
        record = evidence[0]
        self.assertIn('合成市场甲发布新的电机准入说明', record['shared_fragments'])
        # 标题不参与相似度，正文完全相同的转载在正文片段上没有差异
        self.assertEqual(record['only_a'], [])
        self.assertEqual(record['only_b'], [])
        runs = self.content_store.runs()
        self.assertEqual(runs[0]['mode'], 'batch')
        self.assertEqual(runs[0]['rule_version'], 'rules-1.0.0')

    def test_boilerplate_removed_but_paths_and_order_kept(self):
        self.batch_run()
        article = self.content_store.current_articles()['north-1']
        paths = [block['path'] for block in article['blocks']]
        self.assertEqual(paths, ['/html[1]/body[1]/main[1]/h1[1]',
                                 '/html[1]/body[1]/main[1]/article[1]/p[1]'])
        self.assertTrue(all('nav' not in block['path'] for block in article['blocks']))
        self.assertTrue(all(item['path'].endswith('nav[1]') or item['path'].endswith('footer[1]')
                            for item in article['dropped']))
        # 短公告正文保留
        short = self.content_store.current_articles()['north-short']
        self.assertIn('电机登记已结束。', [block['text'] for block in short['blocks']])
        # 代码示例样本不足，保守策略不剥离
        code = self.content_store.current_articles()['sample-code']
        self.assertEqual(code['dropped'], [])

    def test_identical_bytes_force_same_observation_and_edge(self):
        body = (FIXTURES / 'corpus' / 'north-1.html').read_bytes()
        entries = [
            {'id': 'dup-a', 'site': 'north', 'kind': 'detail', 'path': 'dup-a'},
            {'id': 'dup-b', 'site': 'south', 'kind': 'detail', 'path': 'dup-b'},
        ]
        result = self.pipeline.ingest(entries, 'batch', '2026-11-12T00:00:00Z',
                                      raw_by_id={'dup-a': body, 'dup-b': body})
        self.assertEqual(result['stats']['forced_identical_pairs'], 1)
        evidence = self.content_store.evidence('dup-a', 'dup-b')[0]
        self.assertEqual(evidence['relation'], 'same')
        self.assertIn('identical_bytes', evidence['reasons'])
        # 原文对象只有一份，观察两条
        digests = {row['digest'] for row in self.raw_store.observations()
                   if row['source'] in ('dup-a', 'dup-b')}
        self.assertEqual(len(digests), 1)

    def test_ambiguous_chain_is_persisted_not_force_merged(self):
        shared_ab = ''.join(f'甲篇共享叙述内容编号{i:02d}。' for i in range(12))
        shared_bc = ''.join(f'乙篇共享叙述内容编号{i:02d}。' for i in range(12))
        template = ('<html><body><main><h1>{title}</h1><article><p>{body}</p>'
                    '</article></main></body></html>')
        bodies = {
            'amb-a': shared_ab + '甲篇独有的补充说明段落。',
            'amb-b': shared_ab + shared_bc,
            'amb-c': shared_bc + '丙篇独有结尾说明。',
        }
        raw_by_id = {
            article_id: template.format(title=article_id, body=body).encode('utf-8')
            for article_id, body in bodies.items()}
        entries = [{'id': article_id, 'site': 'amb', 'kind': 'detail', 'path': article_id}
                   for article_id in bodies]
        # 单次只有三页，站点样本不足走保守策略，恰好保留全部共享文本用于歧义验证
        result = self.pipeline.ingest(entries, 'batch', '2026-11-12T00:00:00Z',
                                      raw_by_id=raw_by_id)
        ambiguous = [group for group in result['groups'] if group['status'] == 'ambiguous']
        self.assertEqual(len(ambiguous), 1)
        self.assertEqual(ambiguous[0]['members'], ['amb-a', 'amb-b', 'amb-c'])
        self.assertEqual(ambiguous[0]['missing_direct_edges'], [('amb-a', 'amb-c')])
        stored = self.content_store.group_of('amb-b')
        self.assertEqual(stored['status'], 'ambiguous')
        # 缺失直接证据的 a-c 对也必须有证据行，解释为什么不能宣布同稿
        evidence = { (e['article_a'], e['article_b']): e for e in self.content_store.evidence()}
        self.assertEqual(evidence[('amb-a', 'amb-c')]['relation'], 'different')
        self.assertLess(evidence[('amb-a', 'amb-c')]['score'], self.truth['threshold'])


class TemplateRevisionTests(PipelineTestBase):
    def _site_pages(self, nav_text):
        base = ('<html><body><nav>{nav}</nav><main><h1>{title}</h1>'
                '<article><p>{body}</p></article></main><footer>{nav}</footer></body></html>')
        bodies = {
            1: '电机准入说明登记窗口十一月开启，企业需提交授权书与历史证书扫描件。',
            2: '食品标签咨询会改期至下周，议程覆盖成分表翻译与现场核验流程。',
            3: '纺织原料抽检安排公布，企业可在线预约抽样时段并获取报告编号。',
        }
        return {
            f'rev-{i}': base.format(
                nav=nav_text, title=f'rev-{i}', body=bodies[i]).encode('utf-8')
            for i in range(1, 4)
        }

    def test_site_revision_invalidates_old_template_and_keeps_history(self):
        old_raw = self._site_pages('旧版导航 服务声明 联系方式')
        entries = [{'id': key, 'site': 'rev', 'kind': 'detail', 'path': key}
                   for key in old_raw]
        first = self.pipeline.ingest(entries, 'batch', '2026-11-01T00:00:00Z',
                                     raw_by_id=old_raw)
        old_fingerprint = first['models'][0]['fingerprint']
        article = self.content_store.current_articles()['rev-1']
        self.assertEqual(len(article['blocks']), 2)
        old_digest = article['raw_digest']
        old_versions = self.content_store.content_versions('rev-1')
        self.assertEqual(len(old_versions), 1)

        # 站点改版：导航与页脚换成新模板，只重新采集其中一篇；其余两篇须自动重算
        new_raw = {
            key: value.replace('旧版导航 服务声明 联系方式'.encode('utf-8'),
                               '新版导航 改版声明 备案编号'.encode('utf-8'))
            for key, value in old_raw.items()}
        partial = [entries[0]]
        second = self.pipeline.ingest(partial, 'incremental', '2026-11-12T00:00:00Z',
                                      raw_by_id={'rev-1': new_raw['rev-1']})
        self.assertEqual(second['stats']['model_versions_changed'][0]['site'], 'rev')
        self.assertEqual(set(second['stats']['reprocessed_articles']),
                         {'rev-1', 'rev-2', 'rev-3'})

        # 旧模板版本与旧正文版本都保留，可追溯
        history = self.content_store.model_history('rev', 'detail')
        self.assertEqual(len(history), 2)
        self.assertNotEqual(history[-1]['fingerprint'], old_fingerprint)
        for article_id in ('rev-1', 'rev-2', 'rev-3'):
            versions = self.content_store.content_versions(article_id)
            self.assertEqual(len(versions), 2)
            self.assertNotEqual(versions[0]['model_fingerprint'], versions[1]['model_fingerprint'])

        # 未重新采集的文章从原文对象重算，原始字节与既有观察均未删除
        rev2 = self.content_store.current_articles()['rev-2']
        self.assertTrue(all('新版导航' not in block['text'] for block in rev2['blocks']))
        self.assertTrue(all(item['text'].startswith('新版导航') or '服务声明' in item['text']
                            for item in rev2['dropped']))
        self.assertEqual(self.content_store.read_object(old_digest), old_raw['rev-1'])
        observations = [row for row in self.raw_store.observations()
                        if row['source'].startswith('rev-')]
        # rev-1 改版前后字节不同 -> 同一来源保留两条观察；rev-2/rev-3 各一条
        self.assertEqual(len(observations), 4)
        rev1_rows = [row for row in observations if row['source'] == 'rev-1']
        self.assertEqual(len(rev1_rows), 2)
        self.assertEqual({row['digest'] for row in rev1_rows},
                         {self.content_store.current_articles()['rev-1']['raw_digest'], old_digest})

        # 再整批重算：新模板学成后首次应用，剥离导航后三篇恢复为独立内容，
        # 归组只基于保留下来的正文片段
        third = self.pipeline.ingest(entries, 'batch', '2026-11-12T00:00:00Z',
                                     raw_by_id=new_raw)
        self.assertEqual(set(third['stats']['reprocessed_articles']),
                         {'rev-1', 'rev-2', 'rev-3'})
        self.assertEqual(
            sorted(group['members'] for group in third['groups']
                   if group['members'][0].startswith('rev-')),
            [['rev-1'], ['rev-2'], ['rev-3']])

        # 模板稳定后再次整批重算，不应再产生新版本或失效重算
        fourth = self.pipeline.ingest(entries, 'batch', '2026-11-12T00:00:00Z',
                                      raw_by_id=new_raw)
        self.assertEqual(fourth['stats']['reprocessed_articles'], [])
        self.assertEqual(fourth['stats']['model_versions_changed'], [])
        # 三次模板版本（旧模板/保守/新模板）对应每篇三个正文版本，全部留痕
        for article_id in ('rev-1', 'rev-2', 'rev-3'):
            self.assertEqual(len(self.content_store.content_versions(article_id)), 3)
        fifth = self.pipeline.ingest(entries, 'batch', '2026-11-12T00:00:00Z',
                                     raw_by_id=new_raw)
        self.assertEqual(fifth['stats']['reprocessed_articles'], [])
        for article_id in ('rev-1', 'rev-2', 'rev-3'):
            self.assertEqual(len(self.content_store.content_versions(article_id)), 3)


if __name__ == '__main__':
    unittest.main()
