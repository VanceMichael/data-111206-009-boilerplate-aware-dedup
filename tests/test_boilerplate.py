import unittest

from boilerplate import apply_model, is_core_path, is_protected, learn_model
from text_source import extract


def page(body_text, nav='站点导航 服务声明 联系方式', title='标题'):
    return (
        '<html><body>'
        f'<nav>{nav}</nav>'
        f'<main><h1>{title}</h1><article><p>{body_text}</p></article></main>'
        f'<footer>{nav}</footer>'
        '</body></html>'
    )


class BoilerplateTests(unittest.TestCase):
    def test_conservative_when_samples_insufficient(self):
        pages = [extract(page('完全不同的正文内容第一篇。'))]
        model = learn_model('s1', 'detail', pages)
        self.assertEqual(model['status'], 'conservative')
        self.assertEqual(model['rules'], [])
        kept, dropped = apply_model(model, pages[0])
        self.assertEqual(kept, pages[0])
        self.assertEqual(dropped, [])

    def test_nav_and_footer_learned_across_pages(self):
        pages = [
            extract(page('第一篇正文讲电机登记窗口的安排细节。', title='甲')),
            extract(page('第二篇正文讲食品标签咨询会议的内容。', title='乙')),
            extract(page('第三篇正文讲纺织原料抽检的时间地点。', title='丙')),
        ]
        model = learn_model('s1', 'detail', pages)
        self.assertEqual(model['status'], 'learned')
        rule_paths = {rule['path'] for rule in model['rules']}
        self.assertIn('/html[1]/body[1]/nav[1]', rule_paths)
        self.assertIn('/html[1]/body[1]/footer[1]', rule_paths)
        kept, dropped = apply_model(model, pages[0])
        kept_texts = [block['text'] for block in kept]
        self.assertIn('第一篇正文讲电机登记窗口的安排细节。', kept_texts)
        self.assertNotIn(kept_texts[0] if False else '站点导航 服务声明 联系方式', kept_texts)
        # 保留块仍带 DOM 路径与顺序
        self.assertTrue(all('path' in block and 'ordinal' in block for block in kept))
        self.assertEqual([block['ordinal'] for block in kept], sorted(block['ordinal'] for block in kept))
        self.assertEqual({item['reason'] for item in dropped}, {'template_boilerplate'})

    def test_short_announcement_body_is_never_stripped(self):
        # 短公告在多页出现也不能被当成模板删掉
        pages = [
            extract(page('电机登记已结束。', title='甲')),
            extract(page('电机登记已结束。', title='乙')),
            extract(page('电机登记已结束。', title='丙')),
        ]
        model = learn_model('s1', 'detail', pages)
        kept, _dropped = apply_model(model, pages[0])
        self.assertIn('电机登记已结束。', [block['text'] for block in kept])
        self.assertTrue(all(is_protected(block) for block in kept if block['text'] == '电机登记已结束。'))

    def test_core_region_blocks_are_protected(self):
        blocks = extract('<main><article><p>万元</p></article></main>')
        self.assertTrue(all(is_core_path(block['path']) for block in blocks))

    def test_list_and_detail_models_are_separate(self):
        list_pages = [
            extract('<body><div class="toolbar">列表筛选工具栏 全部市场 只看本周 订阅列表更新</div><main><article><p>列表正文甲。</p></article></main></body>'),
            extract('<body><div class="toolbar">列表筛选工具栏 全部市场 只看本周 订阅列表更新</div><main><article><p>列表正文乙。</p></article></main></body>'),
            extract('<body><div class="toolbar">列表筛选工具栏 全部市场 只看本周 订阅列表更新</div><main><article><p>列表正文丙。</p></article></main></body>'),
        ]
        detail_pages = [
            extract(page('详情正文甲内容。')),
            extract(page('详情正文乙内容。')),
            extract(page('详情正文丙内容。')),
        ]
        list_model = learn_model('s1', 'list', list_pages)
        detail_model = learn_model('s1', 'detail', detail_pages)
        self.assertNotEqual(list_model['fingerprint'], detail_model['fingerprint'])
        # 列表工具栏规则不会出现在详情模型里
        self.assertTrue(any('div' in rule['path'] for rule in list_model['rules']))
        self.assertFalse(any('div' in rule['path'] for rule in detail_model['rules']))
        # 详情页应用列表模型不会误删（规则键含路径+文本，互不命中）
        kept, dropped = apply_model(list_model, detail_pages[0])
        self.assertEqual(len(dropped), 0)
        self.assertIn('详情正文甲内容。', [block['text'] for block in kept])

    def test_same_path_but_different_text_is_not_a_rule(self):
        pages = [
            extract(page('页面甲独有的一段足够长的正文叙述内容。', title='甲')),
            extract(page('页面乙独有的一段足够长的正文叙述内容。', title='乙')),
            extract(page('页面丙独有的一段足够长的正文叙述内容。', title='丙')),
        ]
        model = learn_model('s1', 'detail', pages)
        # 正文在 article 内受保护；即便路径相同文本不同也不会产生规则
        self.assertFalse(any('article' in rule['path'] for rule in model['rules']))
