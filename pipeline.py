"""采集编排：模板学习、正文版本、候选判断与归组的同一规则流程。

批量与增量共用 :meth:`Pipeline.ingest`：

* 模板只从本次重新采集页面的原始文本块、按 (站点, 页面类型) 学习；库内老版本
  是旧模板的产物，不混入学习样本，避免改版后的新模板被旧页面支持率稀释。
* 本次样本不足的站点沿用最近一次已学习模型（旧规则仍命中时），页面数波动不会
  触发误失效；旧规则在新页面全部失配则判定改版，进入保守状态等待新样本。
* 模板指纹变化（站点改版）时旧版本保留，受影响文章自动进入待重算列表，
  用原文对象重新抽取并追加正文版本；原始字节与既有观察不删不改。
* 候选对来自持久化倒排索引，编辑约束对与相同字节对强制纳入，
  实际候选/打分数量写入运行统计并对外报告。
* 归组规则纯确定性，逐篇入库、乱序入库与整批重算只允许产生相同结果。
"""

import hashlib

from boilerplate import RULE_VERSION, apply_model, learn_model
from near_duplicate import (
    DEFAULT_MERGE_THRESHOLD,
    ShingleIndex,
    build_document,
    build_groups,
    score_edges,
)
from text_source import extract


class Pipeline:
    def __init__(self, raw_store, content_store, threshold=DEFAULT_MERGE_THRESHOLD,
                 rule_version=RULE_VERSION):
        self.raw_store = raw_store
        self.content_store = content_store
        self.threshold = threshold
        self.rule_version = rule_version

    def install_constraints(self, constraints, seen_at):
        return self.content_store.replace_constraints(constraints, self.rule_version, seen_at)

    def ingest(self, entries, mode, seen_at, raw_by_id=None):
        """执行一次采集判断。

        entries: manifest 条目（id/site/kind/path）。
        raw_by_id: 可选的 {文章 id: 原始字节}，提供时不再读磁盘。
        mode: 'batch' 或 'incremental'，仅用于运行留痕，不改变规则。
        """
        raw_by_id = dict(raw_by_id or {})
        # 1) 原文落库：相同字节共享 object，观察按 (来源, 摘要) 合并，永不改写原件
        for entry in entries:
            body = raw_by_id.get(entry['id'])
            if body is None:
                from pathlib import Path
                body = Path(entry['path']).read_bytes()
                raw_by_id[entry['id']] = body
            self.raw_store.save(entry['id'], body,
                                {'site': entry['site'], 'kind': entry['kind']}, seen_at)

        # 2) 新条目抽取原始文本块，按 (站点,类型) 用本次原始页面学习模板；
        #    样本不足的站点沿用最近一次已学习模型，避免页面数波动引发误失效
        fresh_blocks = {}
        for entry in entries:
            fresh_blocks[entry['id']] = extract(raw_by_id[entry['id']].decode('utf-8'))
        sample_pages = self._collect_learning_samples(entries, fresh_blocks)

        learned_models, model_changes = {}, []
        for (site, kind), pages in sorted(sample_pages.items()):
            model = learn_model(site, kind, pages)
            if model['status'] == 'conservative':
                previous = self.content_store.latest_models().get((site, kind))
                # 仅当旧模板在本次原始页面上仍能命中规则文本时才沿用，
                # 否则视为站点已改版：保持保守模型，等待新样本重新学习
                if previous is not None and previous['status'] == 'learned' and \
                        self._template_still_matches(previous, pages):
                    model = previous
            _version, changed = self.content_store.save_model(model, seen_at)
            learned_models[(site, kind)] = model
            if changed:
                model_changes.append({'site': site, 'kind': kind,
                                      'fingerprint': model['fingerprint'],
                                      'status': model['status']})

        # 3) 模板改版后旧正文失效：连同新文章一起按最新模板重算，追加新版本
        latest_models = self.content_store.latest_models()
        existing = self.content_store.current_articles()
        existing_ids = set(existing)
        stale = set(self.content_store.stale_articles(latest_models))
        reprocessed, processed_new = [], []
        for entry in entries:
            article_id = entry['id']
            raw_digest = hashlib.sha256(raw_by_id[article_id]).hexdigest()
            if article_id not in existing_ids:
                processed_new.append(article_id)
            # 仅当模板指纹或原文摘要变化时才追加正文版本，稳定重采不产生冗余版本
            elif article_id in stale or existing[article_id]['raw_digest'] != raw_digest:
                reprocessed.append(article_id)
            else:
                continue
            self._rebuild_article(entry, fresh_blocks[article_id],
                                  learned_models[(entry['site'], entry['kind'])],
                                  raw_digest, seen_at)
        # 本次未重新采集、但模板已改版的老文章：从原文对象重算，原件只读
        for article_id in sorted(stale - set(fresh_blocks)):
            self._rebuild_from_raw(article_id, latest_models, seen_at)
            reprocessed.append(article_id)

        # 4) 载入当前全部正文版本，构建内存索引并与持久化索引核对候选集合
        articles = self.content_store.current_articles()
        documents = {article_id: record['document'] for article_id, record in articles.items()}
        index = ShingleIndex()
        for article_id in sorted(articles):
            index.add(documents[article_id])
        persisted_pairs = set(self.content_store.candidate_pairs_from_index())
        memory_pairs = set(index.candidate_pairs())
        if persisted_pairs != memory_pairs:
            raise RuntimeError('持久化索引与内存索引候选不一致，拒绝在漏判风险下继续')

        constraints = self.content_store.load_constraints()
        constraints.setdefault('threshold', self.threshold)
        forced_identical = self.content_store.identical_digest_pairs()
        edges, stats = score_edges(
            documents, index, self.threshold, constraints, forced_identical,
            rule_version=self.rule_version)
        stats['mode'] = mode
        stats['model_versions_changed'] = model_changes
        stats['new_articles'] = sorted(processed_new)
        stats['reprocessed_articles'] = sorted(set(reprocessed))
        stats['index_candidate_pairs'] = len(memory_pairs)
        stats['forced_identical_pairs'] = len(forced_identical)

        # 5) 团约束归组并固化证据；每次运行留痕，历史运行与正文版本均可回溯
        groups = build_groups(documents, edges)
        run_id = self.content_store.start_run(
            mode, self.threshold, stats, self.rule_version, seen_at)
        self.content_store.save_evidence(run_id, articles, edges)
        self.content_store.save_groups(run_id, groups)
        return {
            'run_id': run_id,
            'rule_version': self.rule_version,
            'threshold': self.threshold,
            'stats': stats,
            'models': [
                {'site': site, 'kind': kind, 'status': model['status'],
                 'page_count': model['page_count'], 'rule_count': len(model['rules']),
                 'fingerprint': model['fingerprint']}
                for (site, kind), model in sorted(learned_models.items())
            ],
            'groups': groups,
        }

    def _collect_learning_samples(self, entries, fresh_blocks):
        """模板学习只使用本次重新采集页面的原始文本块。

        库内老版本的保留块/剥离块是旧模板的产物，混入学习样本会让改版后的新
        模板因支持率被旧页面稀释而永远学不出来；老页面在新模板学成后再统一重算。
        """
        pages = {}
        for entry in entries:
            pages.setdefault((entry['site'], entry['kind']), []).append(
                fresh_blocks[entry['id']])
        return pages

    @staticmethod
    def _template_still_matches(model, pages):
        """旧模板规则文本是否仍在本次原始页面中出现，用于区分小批量与站点改版。"""
        rule_keys = {(rule['path'], rule['text']) for rule in model['rules']}
        for blocks in pages:
            if any((block['path'], block['text']) in rule_keys for block in blocks):
                return True
        return False

    def _rebuild_article(self, entry, raw_blocks, model, raw_digest, seen_at):
        kept, dropped = apply_model(model, raw_blocks)
        document = build_document(entry['id'], kept)
        self.content_store.save_content_version(
            entry['id'], raw_digest, entry['site'], entry['kind'],
            model, kept, dropped, document, seen_at)

    def _rebuild_from_raw(self, article_id, latest_models, seen_at):
        current = self.content_store.current_articles()[article_id]
        model = latest_models[(current['site'], current['kind'])]
        body = self.content_store.read_object(current['raw_digest'])
        raw_blocks = extract(body.decode('utf-8'))
        kept, dropped = apply_model(model, raw_blocks)
        document = build_document(article_id, kept)
        self.content_store.save_content_version(
            article_id, current['raw_digest'], current['site'], current['kind'],
            model, kept, dropped, document, seen_at)
