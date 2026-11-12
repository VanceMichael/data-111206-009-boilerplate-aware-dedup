"""正文判别与近重复结果的持久化。

与 raw_store.py 共用同一个 SQLite 文件但只新增表，不改写原文表：

* objects / observations 仍由 RawStore 管理，原始字节与既有观察永不删除。
* template_models 保存每个 (站点, 页面类型) 的全部模板版本，站点改版后
  指纹变化即新增版本，旧版本保留并可让旧正文标记为失效重算。
* content_versions 是只追加的正文版本表；article_current 指向当前版本，
  版本切换与倒排索引更新在同一事务内完成，不会漏掉待重判文章。
* judge_runs / pair_evidence / content_groups / group_members 保存每次
  判断运行的证据与归组结果，历史运行全部可查询，当前结果取最新运行。
"""

import json
import sqlite3
from pathlib import Path


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _decode(text):
    return json.loads(text)


class ContentStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=10)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS template_models (
                site TEXT NOT NULL, kind TEXT NOT NULL, version INTEGER NOT NULL,
                rule_version TEXT NOT NULL, status TEXT NOT NULL,
                page_count INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                rules_json TEXT NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(site, kind, version));
            CREATE TABLE IF NOT EXISTS content_versions (
                version_id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL, raw_digest TEXT NOT NULL,
                site TEXT NOT NULL, kind TEXT NOT NULL,
                model_fingerprint TEXT NOT NULL, rule_version TEXT NOT NULL,
                blocks_json TEXT NOT NULL, dropped_json TEXT NOT NULL,
                fragments_json TEXT NOT NULL, shingles_json TEXT NOT NULL,
                content_length INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS article_current (
                article_id TEXT PRIMARY KEY, version_id INTEGER NOT NULL,
                raw_digest TEXT NOT NULL, site TEXT NOT NULL, kind TEXT NOT NULL,
                model_fingerprint TEXT NOT NULL,
                FOREIGN KEY(version_id) REFERENCES content_versions(version_id));
            CREATE TABLE IF NOT EXISTS shingle_postings (
                shingle TEXT NOT NULL, article_id TEXT NOT NULL,
                version_id INTEGER NOT NULL,
                PRIMARY KEY(shingle, article_id));
            CREATE TABLE IF NOT EXISTS editor_constraints (
                article_a TEXT NOT NULL, article_b TEXT NOT NULL,
                relation TEXT NOT NULL, rule_version TEXT NOT NULL,
                PRIMARY KEY(article_a, article_b));
            CREATE TABLE IF NOT EXISTS judge_runs (
                run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL, rule_version TEXT NOT NULL,
                threshold REAL NOT NULL, stats_json TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pair_evidence (
                run_id INTEGER NOT NULL, article_a TEXT NOT NULL, article_b TEXT NOT NULL,
                version_a INTEGER NOT NULL, version_b INTEGER NOT NULL,
                relation TEXT NOT NULL, score REAL NOT NULL, threshold REAL NOT NULL,
                shared_json TEXT NOT NULL, only_a_json TEXT NOT NULL, only_b_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL, rule_version TEXT NOT NULL,
                PRIMARY KEY(run_id, article_a, article_b));
            CREATE TABLE IF NOT EXISTS content_groups (
                run_id INTEGER NOT NULL, group_id TEXT NOT NULL,
                status TEXT NOT NULL, representative TEXT NOT NULL,
                selection_rule TEXT NOT NULL, members_json TEXT NOT NULL,
                missing_edges_json TEXT NOT NULL,
                PRIMARY KEY(run_id, group_id));
            CREATE TABLE IF NOT EXISTS group_members (
                run_id INTEGER NOT NULL, group_id TEXT NOT NULL,
                article_id TEXT NOT NULL, is_representative INTEGER NOT NULL,
                PRIMARY KEY(run_id, group_id, article_id));
            CREATE INDEX IF NOT EXISTS idx_postings_article
                ON shingle_postings(article_id);
            CREATE INDEX IF NOT EXISTS idx_versions_article
                ON content_versions(article_id);
        """)

    def close(self):
        self.connection.close()

    # -- 原文读取（复用 RawStore 的 objects 表） --------------------------------

    def read_object(self, digest):
        row = self.connection.execute(
            'SELECT body FROM objects WHERE digest=?', (digest,)).fetchone()
        if row is None:
            raise KeyError(digest)
        return bytes(row[0])

    # -- 编辑约束 -------------------------------------------------------------

    def replace_constraints(self, constraints, rule_version, seen_at):
        """用编辑提供的最新样例全量替换约束表（样例量少，且必须可解释来源）。"""
        rows = []
        for pair in constraints.get('confirmed_same', []):
            left, right = sorted(pair)
            rows.append((left, right, 'same', rule_version))
        for pair in constraints.get('confirmed_different', []):
            left, right = sorted(pair)
            rows.append((left, right, 'blocked', rule_version))
        with self.connection:
            self.connection.execute('DELETE FROM editor_constraints')
            self.connection.executemany(
                'INSERT INTO editor_constraints VALUES(?,?,?,?)', rows)
        return len(rows)

    def load_constraints(self):
        rows = self.connection.execute(
            'SELECT article_a,article_b,relation FROM editor_constraints ORDER BY article_a,article_b')
        result = {'confirmed_same': [], 'confirmed_different': []}
        for left, right, relation in rows:
            result['confirmed_same' if relation == 'same' else 'confirmed_different'].append([left, right])
        return result

    # -- 模板模型版本 ----------------------------------------------------------

    def save_model(self, model, seen_at):
        """保存模板版本；指纹未变则复用当前版本，改版后新增版本，旧版本保留。"""
        existing = self.connection.execute(
            'SELECT version FROM template_models WHERE site=? AND kind=? '
            'ORDER BY version DESC LIMIT 1', (model['site'], model['kind'])).fetchone()
        if existing is not None:
            fingerprint_row = self.connection.execute(
                'SELECT fingerprint,version FROM template_models WHERE site=? AND kind=? '
                'ORDER BY version DESC LIMIT 1', (model['site'], model['kind'])).fetchone()
            if fingerprint_row[0] == model['fingerprint']:
                return fingerprint_row[1], False
        version = (existing[0] + 1) if existing else 1
        with self.connection:
            self.connection.execute(
                'INSERT INTO template_models VALUES(?,?,?,?,?,?,?,?,?)',
                (model['site'], model['kind'], version, model['rule_version'],
                 model['status'], model['page_count'], model['fingerprint'],
                 _encode(model['rules']), seen_at))
        return version, True

    def latest_models(self):
        rows = self.connection.execute(
            'SELECT site,kind,version,rule_version,status,page_count,fingerprint,rules_json '
            'FROM template_models t WHERE version=(SELECT MAX(version) FROM template_models '
            'WHERE site=t.site AND kind=t.kind)')
        models = {}
        for site, kind, version, rule_version, status, page_count, fingerprint, rules_json in rows:
            models[(site, kind)] = {
                'site': site, 'kind': kind, 'version': version,
                'rule_version': rule_version, 'status': status,
                'page_count': page_count, 'fingerprint': fingerprint,
                'rules': _decode(rules_json),
            }
        return models

    def model_history(self, site, kind):
        rows = self.connection.execute(
            'SELECT version,status,page_count,fingerprint,created_at,rules_json '
            'FROM template_models WHERE site=? AND kind=? ORDER BY version', (site, kind))
        return [dict(zip(('version', 'status', 'page_count', 'fingerprint', 'created_at', 'rules'),
                         (a, b, c, d, e, _decode(f)))) for a, b, c, d, e, f in rows]

    # -- 正文版本与索引 --------------------------------------------------------

    def save_content_version(self, article_id, raw_digest, site, kind, model,
                             blocks, dropped, document, seen_at):
        """追加一个正文版本，并在同一事务内切换当前版本与倒排索引。"""
        with self.connection:
            cursor = self.connection.execute(
                'INSERT INTO content_versions(article_id,raw_digest,site,kind,'
                'model_fingerprint,rule_version,blocks_json,dropped_json,'
                'fragments_json,shingles_json,content_length,created_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (article_id, raw_digest, site, kind, model['fingerprint'],
                 model['rule_version'], _encode(blocks), _encode(dropped),
                 _encode(document['fragments']), _encode(sorted(document['shingles'])),
                 document['content_length'], seen_at))
            version_id = cursor.lastrowid
            self.connection.execute(
                'INSERT INTO article_current VALUES(?,?,?,?,?,?) '
                'ON CONFLICT(article_id) DO UPDATE SET version_id=excluded.version_id,'
                'raw_digest=excluded.raw_digest,site=excluded.site,kind=excluded.kind,'
                'model_fingerprint=excluded.model_fingerprint',
                (article_id, version_id, raw_digest, site, kind, model['fingerprint']))
            # 索引与版本切换同事务：任何时刻当前文章都有完整索引，不会漏判
            self.connection.execute(
                'DELETE FROM shingle_postings WHERE article_id=?', (article_id,))
            self.connection.executemany(
                'INSERT INTO shingle_postings(shingle,article_id,version_id) VALUES(?,?,?)',
                [(shingle, article_id, version_id) for shingle in sorted(document['shingles'])])
        return version_id

    def current_articles(self):
        from near_duplicate import split_fragments
        rows = self.connection.execute(
            'SELECT c.article_id,c.version_id,c.raw_digest,c.site,c.kind,c.model_fingerprint,'
            'v.blocks_json,v.dropped_json,v.fragments_json,v.shingles_json,v.content_length '
            'FROM article_current c JOIN content_versions v ON v.version_id=c.version_id '
            'ORDER BY c.article_id')
        articles = {}
        for (article_id, version_id, raw_digest, site, kind, fingerprint,
             blocks_json, dropped_json, fragments_json, shingles_json, content_length) in rows:
            blocks = _decode(blocks_json)
            fragment_paths = []
            for block in blocks:
                fragment_paths.extend(
                    block['path'] for _fragment in split_fragments(block['text']))
            articles[article_id] = {
                'article_id': article_id, 'version_id': version_id,
                'raw_digest': raw_digest, 'site': site, 'kind': kind,
                'model_fingerprint': fingerprint,
                'blocks': blocks, 'dropped': _decode(dropped_json),
                'document': {
                    'article_id': article_id,
                    'fragments': _decode(fragments_json),
                    'fragment_paths': fragment_paths,
                    'shingles': set(_decode(shingles_json)),
                    'content_length': content_length,
                },
            }
        return articles

    def stale_articles(self, models):
        """当前正文指纹与站点最新模板不一致（含改版与尚无模板）的文章。"""
        rows = self.connection.execute(
            'SELECT article_id,site,kind,model_fingerprint FROM article_current ORDER BY article_id')
        stale = []
        for article_id, site, kind, fingerprint in rows:
            model = models.get((site, kind))
            if model is None or model['fingerprint'] != fingerprint:
                stale.append(article_id)
        return stale

    def content_versions(self, article_id):
        rows = self.connection.execute(
            'SELECT version_id,raw_digest,model_fingerprint,rule_version,blocks_json,'
            'dropped_json,created_at FROM content_versions WHERE article_id=? ORDER BY version_id',
            (article_id,))
        return [dict(zip(('version_id', 'raw_digest', 'model_fingerprint', 'rule_version',
                          'blocks', 'dropped', 'created_at'),
                         (a, b, c, d, _decode(e), _decode(f), g)))
                for a, b, c, d, e, f, g in rows]

    def candidate_pairs_from_index(self):
        """从持久化倒排索引还原候选对，避免逐对扫描整库。"""
        rows = self.connection.execute(
            'SELECT s1.article_id, s2.article_id FROM shingle_postings s1 '
            'JOIN shingle_postings s2 ON s1.shingle=s2.shingle '
            'WHERE s1.article_id < s2.article_id GROUP BY s1.article_id, s2.article_id '
            'ORDER BY s1.article_id, s2.article_id')
        return [(left, right) for left, right in rows]

    def identical_digest_pairs(self):
        """相同原始字节的不同文章：直接合并观察的强制同稿边。"""
        rows = self.connection.execute(
            'SELECT a1.article_id, a2.article_id FROM article_current a1 '
            'JOIN article_current a2 ON a1.raw_digest=a2.raw_digest '
            'WHERE a1.article_id < a2.article_id ORDER BY a1.article_id, a2.article_id')
        return {(left, right) for left, right in rows}

    # -- 判断运行、证据与内容组 -----------------------------------------------

    def start_run(self, mode, threshold, stats, rule_version, seen_at):
        with self.connection:
            cursor = self.connection.execute(
                'INSERT INTO judge_runs(mode,rule_version,threshold,stats_json,created_at) '
                'VALUES(?,?,?,?,?)', (mode, rule_version, threshold, _encode(stats), seen_at))
        return cursor.lastrowid

    def save_evidence(self, run_id, articles, edges):
        rows = []
        for (left, right), evidence in sorted(edges.items()):
            rows.append((run_id, left, right,
                         articles[left]['version_id'], articles[right]['version_id'],
                         evidence['relation'], evidence['score'], evidence['threshold'],
                         _encode(evidence['shared_fragments']),
                         _encode(evidence['only_a']), _encode(evidence['only_b']),
                         _encode(evidence['reasons']), evidence['rule_version']))
        with self.connection:
            self.connection.executemany(
                'INSERT INTO pair_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)', rows)
    def save_groups(self, run_id, groups):
        group_rows, member_rows = [], []
        for group in groups:
            group_rows.append((run_id, group['group_id'], group['status'],
                               group['representative'], group['selection_rule'],
                               _encode(group['members']), _encode(group['missing_direct_edges'])))
            for article_id in group['members']:
                member_rows.append((run_id, group['group_id'], article_id,
                                    1 if article_id == group['representative'] else 0))
        with self.connection:
            self.connection.executemany(
                'INSERT INTO content_groups VALUES(?,?,?,?,?,?,?)', group_rows)
            self.connection.executemany(
                'INSERT INTO group_members VALUES(?,?,?,?)', member_rows)

    def latest_run_id(self):
        row = self.connection.execute('SELECT MAX(run_id) FROM judge_runs').fetchone()
        return row[0]

    def runs(self):
        rows = self.connection.execute(
            'SELECT run_id,mode,rule_version,threshold,stats_json,created_at '
            'FROM judge_runs ORDER BY run_id')
        return [dict(zip(('run_id', 'mode', 'rule_version', 'threshold', 'stats', 'created_at'),
                         (a, b, c, d, _decode(e), f))) for a, b, c, d, e, f in rows]

    def groups(self, run_id=None):
        run_id = run_id if run_id is not None else self.latest_run_id()
        if run_id is None:
            return []
        rows = self.connection.execute(
            'SELECT group_id,status,representative,selection_rule,members_json,missing_edges_json '
            'FROM content_groups WHERE run_id=? ORDER BY group_id', (run_id,))
        return [dict(zip(('group_id', 'status', 'representative', 'selection_rule',
                          'members', 'missing_direct_edges'),
                         (a, b, c, d, _decode(e), _decode(f))))
                for a, b, c, d, e, f in rows]

    def group_of(self, article_id, run_id=None):
        run_id = run_id if run_id is not None else self.latest_run_id()
        row = self.connection.execute(
            'SELECT g.group_id,g.status,g.representative,g.selection_rule,'
            'g.members_json,g.missing_edges_json FROM group_members m '
            'JOIN content_groups g ON g.run_id=m.run_id AND g.group_id=m.group_id '
            'WHERE m.run_id=? AND m.article_id=?', (run_id, article_id)).fetchone()
        if row is None:
            return None
        return dict(zip(('group_id', 'status', 'representative', 'selection_rule',
                         'members', 'missing_direct_edges'),
                        (row[0], row[1], row[2], row[3], _decode(row[4]), _decode(row[5]))))

    def evidence(self, left=None, right=None, run_id=None):
        run_id = run_id if run_id is not None else self.latest_run_id()
        if run_id is None:
            return []
        query = ('SELECT article_a,article_b,version_a,version_b,relation,score,threshold,'
                 'shared_json,only_a_json,only_b_json,reasons_json,rule_version '
                 'FROM pair_evidence WHERE run_id=?')
        parameters = [run_id]
        if left is not None and right is not None:
            pair = sorted((left, right))
            query += ' AND article_a=? AND article_b=?'
            parameters.extend(pair)
        query += ' ORDER BY article_a,article_b'
        rows = self.connection.execute(query, parameters)
        return [dict(zip(('article_a', 'article_b', 'version_a', 'version_b', 'relation',
                          'score', 'threshold', 'shared_fragments', 'only_a', 'only_b',
                          'reasons', 'rule_version'),
                         (a, b, c, d, e, f, g, _decode(h), _decode(i), _decode(j),
                          _decode(k), l)))
                for a, b, c, d, e, f, g, h, i, j, k, l in rows]
