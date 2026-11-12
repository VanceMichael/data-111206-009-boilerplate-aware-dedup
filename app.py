import argparse
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from content_store import ContentStore
from pipeline import Pipeline
from raw_store import RawStore


def load_inputs():
    entries = json.loads(Path('fixtures/manifest.json').read_text())
    truth = json.loads(Path('fixtures/truth.json').read_text())
    raw_by_id = {entry['id']: Path(entry['path']).read_bytes() for entry in entries}
    return entries, truth, raw_by_id


def build_pipeline(path, threshold):
    raw_store = RawStore(path)
    content_store = ContentStore(path)
    pipeline = Pipeline(raw_store, content_store, threshold=threshold)
    return raw_store, content_store, pipeline


def demo(store_path):
    entries, truth, raw_by_id = load_inputs()
    seen_at = datetime.now(timezone.utc).isoformat()
    raw_store, content_store, pipeline = build_pipeline(store_path, truth['threshold'])
    try:
        pipeline.install_constraints(truth, seen_at)

        # 整批重算：同一规则版本下的基线结果
        batch = pipeline.ingest(entries, 'batch', seen_at, raw_by_id=raw_by_id)

        # 逐篇入库（固定随机种子打乱顺序）：增量路径
        shuffled = list(entries)
        random.Random(20261112).shuffle(shuffled)
        incremental = None
        for index in range(len(shuffled)):
            chunk = shuffled[:index + 1]
            incremental = pipeline.ingest(
                chunk, 'incremental', seen_at,
                raw_by_id={entry['id']: raw_by_id[entry['id']] for entry in chunk})

        consistent = _snapshot(batch['groups']) == _snapshot(incremental['groups'])
        return {
            '规则版本': batch['rule_version'],
            '合并阈值': batch['threshold'],
            '文章数': len(entries),
            '模板模型': batch['models'],
            '候选比较报告': {
                '倒排索引候选对': batch['stats']['index_candidate_pairs'],
                '编辑约束对': batch['stats']['must_link_pairs'] + batch['stats']['cannot_link_pairs'],
                '相同字节强制对': batch['stats']['forced_identical_pairs'],
                '实际打分候选对': batch['stats']['scored_pairs'],
                '全量逐对比较上限': len(entries) * (len(entries) - 1) // 2,
                '同稿边数': batch['stats']['same_edges'],
            },
            '内容组': [
                {
                    '组': group['group_id'],
                    '状态': group['status'],
                    '代表稿': group['representative'],
                    '成员': group['members'],
                    '缺失直接证据对': group['missing_direct_edges'],
                }
                for group in content_store.groups()
            ],
            '批量与增量一致': consistent,
            '说明': '标题不参与相似度；同稿组内每一对都有直接重合证据，相似链不做传递合并。',
        }
    finally:
        content_store.close()
        raw_store.close()


def query(store_path, article_id=None, pair=None):
    raw_store = RawStore(store_path)
    content_store = ContentStore(store_path)
    try:
        result = {'判断运行': content_store.runs()}
        if article_id is not None:
            result['文章组'] = content_store.group_of(article_id)
            record = content_store.current_articles().get(article_id)
            if record is not None:
                result['正文版本数'] = len(content_store.content_versions(article_id))
                result['保留块'] = record['blocks']
                result['剥离块'] = record['dropped']
        if pair is not None:
            left, right = pair.split(',')
            result['差异证据'] = content_store.evidence(left.strip(), right.strip())
        if article_id is None and pair is None:
            result['内容组'] = content_store.groups()
        return result
    finally:
        content_store.close()
        raw_store.close()


def _snapshot(groups):
    return [(group['group_id'], group['status'], group['representative'],
             tuple(group['members'])) for group in groups]


def main():
    parser = argparse.ArgumentParser(description='正文判别与近重复处理演示')
    parser.add_argument('command', choices=['demo', 'query'])
    parser.add_argument('--output', default='output/raw.sqlite3')
    parser.add_argument('--article', help='query: 查看文章所在内容组与正文版本')
    parser.add_argument('--pair', help='query: 查看两篇文章的重合与差异证据，如 north-1,south-copy')
    args = parser.parse_args()
    if args.command == 'demo':
        print(json.dumps(demo(args.output), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(query(args.output, args.article, args.pair),
                         ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
