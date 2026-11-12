"""按来源站点与页面类型学习模板噪声。

判别只在同一站点、同一页面类型（列表页/详情页/代码示例）的多页样本之间进行，
样本不足时保守处理：不生成任何剥离规则，整页保留。

规则候选是 (DOM 路径, 块文本) 完全相同、且在足够多样本中重复出现的块。
正文区域（main/article 内）与短块（短公告正文、公司名、金额单位等）在学习阶段
即被排除，避免把正文误判成模板。规则版本固定，保证同版本下结果可复算。
"""

import hashlib
from collections import Counter

RULE_VERSION = 'rules-1.0.0'

# 同一 (站点, 页面类型) 至少积累多少个样本才开始学习模板
MIN_PAGES = 3
# 候选块出现的页面占比达到该值才判定为模板
SUPPORT_RATIO = 0.75
# 文本不超过该长度的块视为短块，永不作为模板剥离
SHORT_TEXT_LIMIT = 12
# 位于这些标签内部的块属于正文区域，永不作为模板剥离
CORE_TAGS = ('article', 'main')


def is_core_path(path):
    """块的 DOM 路径是否落在正文容器内。"""
    return any(f'/{tag}[' in path for tag in CORE_TAGS)


def is_protected(block):
    """受保护块：正文区域内的块或短块，学习与剥离阶段都不得删除。"""
    return is_core_path(block['path']) or len(block['text']) <= SHORT_TEXT_LIMIT


def learn_model(site, kind, pages):
    """从同一站点、同一类型的若干页面样本学习模板规则。

    pages 为每一页的文本块列表（text_source.extract 的结果）。
    返回模型字典；样本不足时 status 为 conservative，rules 为空，
    调用方应原样保留全部文本块。
    """
    page_count = len(pages)
    if page_count < MIN_PAGES:
        return {
            'site': site,
            'kind': kind,
            'rule_version': RULE_VERSION,
            'status': 'conservative',
            'page_count': page_count,
            'support_ratio': SUPPORT_RATIO,
            'rules': [],
            # 保守策略的指纹与样本数解耦：积累到可学习页数时规则集才是变化来源
            'fingerprint': f'{RULE_VERSION}:conservative:no-rules',
        }
    document_frequency = Counter()
    for blocks in pages:
        seen = set()
        for block in blocks:
            if is_protected(block):
                continue
            key = (block['path'], block['text'])
            if key not in seen:
                seen.add(key)
                document_frequency[key] += 1
    required = page_count * SUPPORT_RATIO
    rules = [
        {'path': path, 'text': text, 'document_frequency': frequency}
        for (path, text), frequency in sorted(document_frequency.items())
        if frequency >= required
    ]
    fingerprint_seed = '|'.join(f'{rule["path"]}@{rule["text"]}' for rule in rules)
    # 指纹只由规则集决定：新增样本但模板不变时不触发正文失效重算
    fingerprint = f'{RULE_VERSION}:learned:' + hashlib.sha256(
        fingerprint_seed.encode('utf-8')).hexdigest()[:16]
    return {
        'site': site,
        'kind': kind,
        'rule_version': RULE_VERSION,
        'status': 'learned',
        'page_count': page_count,
        'support_ratio': SUPPORT_RATIO,
        'rules': rules,
        'fingerprint': fingerprint,
    }


def apply_model(model, blocks):
    """按模型剥离模板块，保留 DOM 路径、文本与原始顺序。

    返回 (保留块列表, 剥离记录列表)。保守模型不剥离任何块。
    学习阶段的保护条件在剥离阶段再次检查，双重保险。
    """
    if model['status'] != 'learned':
        kept = [dict(block) for block in blocks]
        return kept, []
    rule_keys = {(rule['path'], rule['text']) for rule in model['rules']}
    kept, dropped = [], []
    for block in blocks:
        if not is_protected(block) and (block['path'], block['text']) in rule_keys:
            dropped.append({
                'ordinal': block['ordinal'],
                'path': block['path'],
                'text': block['text'],
                'reason': 'template_boilerplate',
                'model_fingerprint': model['fingerprint'],
            })
        else:
            kept.append(dict(block))
    return kept, dropped
