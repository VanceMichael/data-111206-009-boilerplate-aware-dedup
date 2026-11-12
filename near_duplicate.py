"""近重复检测：内容片段、相似度证据、候选索引与可解释归组。

设计约束（与编辑约定的规则版本绑定）：

* 只对模板剥离后保留下来的内容片段做比较，导航与免责声明不参与相似度。
* 相似度给出可解释证据：重合片段与各自独有片段，而不是一个黑盒分数。
* 候选检索通过片段倒排索引完成，不逐对扫描整库；实际候选比较数量可报告。
* 归组不允许链式传递：甲≈乙、乙≈丙不推出甲≈丙。一个归组必须是团
  （组内任意两篇都直接达到合并阈值），无法满足时保留歧义，不碰巧选第一个。
* 代表稿按完全确定性的规则选择，与入库顺序无关，逐篇入库、乱序、整批重算
  在同一规则版本下结果一致。
"""

import hashlib
import re

RULE_VERSION = 'rules-1.0.0'
SHINGLE_SIZE = 4
DEFAULT_MERGE_THRESHOLD = 0.55
# 低于该分数的候选对不再生成证据，避免证据表被噪声对淹没
EVIDENCE_FLOOR = 0.20

FRAGMENT_PATTERN = re.compile(r'[^，。！？；：、,.!?;:()（）\s]+')

# 标题块（页面名称/文章标题）不参与正文相似度，避免转载只换标题时影响归组
HEADING_TAGS = ('h1', 'h2', 'h3')


def is_heading_path(path):
    return any(f'/{tag}[' in path for tag in HEADING_TAGS)


def split_fragments(text):
    """把块文本按中文/英文标点切成内容片段，空串与纯标点被丢弃。"""
    return FRAGMENT_PATTERN.findall(text)


def shingles_from_fragments(fragments):
    """跨片段拼接后生成字符级 shingle 集合，片段边界用 \\x00 隔开。"""
    joined = '\x00'.join(fragments)
    if len(joined) < SHINGLE_SIZE:
        return {joined} if joined else set()
    return {joined[i:i + SHINGLE_SIZE] for i in range(len(joined) - SHINGLE_SIZE + 1)}


def build_document(article_id, blocks):
    """从保留块构造比较文档，保留块的 DOM 路径与顺序信息。

    标题块只保留在正文版本里，不进入片段与 shingle，防止转载换标题干扰相似度。
    """
    fragments = []
    fragment_paths = []
    for block in blocks:
        if is_heading_path(block['path']):
            continue
        for fragment in split_fragments(block['text']):
            fragments.append(fragment)
            fragment_paths.append(block['path'])
    return {
        'article_id': article_id,
        'fragments': fragments,
        'fragment_paths': fragment_paths,
        'shingles': shingles_from_fragments(fragments),
        'content_length': sum(len(fragment) for fragment in fragments),
    }


def jaccard(left, right):
    """两个集合的 Jaccard 相似度；双方都为空时定义为 0。"""
    if not left and not right:
        return 0.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def compare_documents(left, right):
    """给出一对文档的相似度与重合/差异证据。"""
    score = jaccard(left['shingles'], right['shingles'])
    left_set, right_set = set(left['fragments']), set(right['fragments'])
    shared = sorted(left_set & right_set)
    return {
        'article_a': min(left['article_id'], right['article_id']),
        'article_b': max(left['article_id'], right['article_id']),
        'score': round(score, 6),
        'shared_fragments': shared,
        'only_a': sorted(left_set - right_set),
        'only_b': sorted(right_set - left_set),
    }


class ShingleIndex:
    """片段（shingle）倒排索引：候选只在共享至少一个 shingle 的文章之间产生。"""

    def __init__(self):
        self.postings = {}
        self.documents = {}

    def add(self, document):
        article_id = document['article_id']
        self.remove(article_id)
        self.documents[article_id] = document
        for shingle in document['shingles']:
            self.postings.setdefault(shingle, set()).add(article_id)

    def remove(self, article_id):
        old = self.documents.pop(article_id, None)
        if old is None:
            return
        for shingle in old['shingles']:
            posting = self.postings.get(shingle)
            if posting is not None:
                posting.discard(article_id)
                if not posting:
                    del self.postings[shingle]

    def candidate_pairs(self):
        """索引覆盖的全部候选对（无重复、无自比），按字典序返回。"""
        pairs = set()
        for posting in self.postings.values():
            members = sorted(posting)
            for i, left in enumerate(members):
                for right in members[i + 1:]:
                    pairs.add((left, right))
        return sorted(pairs)

    def candidates_for(self, article_id):
        """单篇新文章的候选对，供增量判断使用。"""
        document = self.documents.get(article_id)
        if document is None:
            return []
        neighbors = set()
        for shingle in document['shingles']:
            neighbors.update(self.postings.get(shingle, ()))
        neighbors.discard(article_id)
        return sorted((min(article_id, other), max(article_id, other)) for other in neighbors)


def canonical_pair(left, right):
    return (left, right) if left <= right else (right, left)


def resolve_constraints(constraints):
    """把编辑确认样例整理成 must_link / cannot_link 规范对。"""
    must_link = {canonical_pair(*pair) for pair in constraints.get('confirmed_same', [])}
    cannot_link = {canonical_pair(*pair) for pair in constraints.get('confirmed_different', [])}
    conflict = must_link & cannot_link
    if conflict:
        raise ValueError(f'编辑约束自相矛盾: {sorted(conflict)}')
    return must_link, cannot_link


def score_edges(documents, index, threshold, constraints, forced_identical=frozenset(),
                rule_version=RULE_VERSION):
    """计算全部需要判断的边与证据，返回 (边字典, 统计信息)。

    边关系分为：
      same          直接达到合并阈值，或被编辑确认同稿/字节完全相同
      different     有共享候选但未达阈值，或被编辑确认不同
      blocked       达到阈值但 cannot_link 禁止合并（保留差异，不自动合并）
    每一条 same 边都有直接证据，归组阶段禁止沿边做传递闭包。
    """
    must_link, cannot_link = resolve_constraints(constraints)
    threshold = constraints.get('threshold', threshold)
    pair_set = set(index.candidate_pairs())
    pair_set.update(must_link)
    pair_set.update(cannot_link)
    pair_set.update(forced_identical)

    edges = {}
    stats = {
        'documents': len(documents),
        'candidate_pairs': len(pair_set),
        'scored_pairs': 0,
        'same_edges': 0,
        'blocked_edges': 0,
        'constraint_pairs': len(must_link) + len(cannot_link),
    }
    for left, right in sorted(pair_set):
        if left not in documents or right not in documents:
            # 约束或历史对引用了当前版本中不存在的文章，跳过并留痕
            stats.setdefault('skipped_missing_pairs', 0)
            stats['skipped_missing_pairs'] += 1
            continue
        evidence = compare_documents(documents[left], documents[right])
        stats['scored_pairs'] += 1
        pair = (left, right)
        if pair in cannot_link:
            relation = 'blocked' if evidence['score'] >= threshold else 'different'
        elif pair in must_link or pair in forced_identical:
            relation = 'same'
        elif evidence['score'] >= threshold:
            relation = 'same'
        else:
            relation = 'different'
        evidence['relation'] = relation
        evidence['threshold'] = threshold
        evidence['rule_version'] = rule_version
        evidence['reasons'] = _edge_reasons(pair, relation, must_link, cannot_link, forced_identical)
        edges[pair] = evidence
        if relation == 'same':
            stats['same_edges'] += 1
        if relation == 'blocked':
            stats['blocked_edges'] += 1
    stats['must_link_pairs'] = len(must_link)
    stats['cannot_link_pairs'] = len(cannot_link)
    return edges, stats


def _edge_reasons(pair, relation, must_link, cannot_link, forced_identical):
    reasons = []
    if pair in forced_identical:
        reasons.append('identical_bytes')
    if pair in must_link:
        reasons.append('editor_confirmed_same')
    if pair in cannot_link:
        reasons.append('editor_confirmed_different')
    if relation == 'same' and not reasons:
        reasons.append('similarity_threshold')
    if relation == 'blocked':
        reasons.append('threshold_overridden_by_editor')
    if relation == 'different' and not reasons:
        reasons.append('below_threshold')
    return reasons


def build_groups(documents, edges):
    """按团约束归组，返回可查询的内容组列表。

    步骤：
      1. 只使用 same 边构造连通分量（same 边本身都有直接证据）。
      2. 检查每个分量是否为团：团内任意两篇之间必须存在 same 边。
      3. 是团则形成唯一归组；否则标记 ambiguous，保留全部成员与缺失的直接
         证据对，不强行拆分也不碰巧取第一个。
      4. 没有 same 边的文章各自成为单篇组。
    代表稿确定性选择：保留内容最长者优先，长度相同则文章 id 字典序最小者。
    """
    same_neighbors = {}
    for (left, right), evidence in edges.items():
        if evidence['relation'] == 'same':
            same_neighbors.setdefault(left, set()).add(right)
            same_neighbors.setdefault(right, set()).add(left)

    visited = set()
    groups = []
    for article_id in sorted(documents):
        if article_id in visited:
            continue
        component = _component_of(article_id, same_neighbors)
        visited.update(component)
        members = sorted(component)
        missing_edges = []
        for i, left in enumerate(members):
            for right in members[i + 1:]:
                if right not in same_neighbors.get(left, set()):
                    missing_edges.append((left, right))
        unique = not missing_edges
        representative = _choose_representative(members, documents)
        groups.append({
            'group_id': _group_id(members),
            'status': 'unique' if unique else 'ambiguous',
            'members': members,
            'representative': representative,
            'missing_direct_edges': missing_edges,
            'selection_rule': 'max_content_length_then_min_article_id',
        })
    return sorted(groups, key=lambda group: group['group_id'])


def _component_of(start, adjacency):
    component = {start}
    stack = [start]
    while stack:
        current = stack.pop()
        for neighbor in adjacency.get(current, ()):
            if neighbor not in component:
                component.add(neighbor)
                stack.append(neighbor)
    return component


def _choose_representative(members, documents):
    return sorted(
        members,
        key=lambda article_id: (-documents[article_id]['content_length'], article_id),
    )[0]


def _group_id(members):
    digest = hashlib.sha256('\x00'.join(sorted(members)).encode('utf-8')).hexdigest()[:12]
    return f'grp_{digest}'
