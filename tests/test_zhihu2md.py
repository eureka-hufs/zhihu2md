#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zhihu2md 的离线测试
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import shutil
import sys
import unittest
import urllib.parse
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import zhihu2md as z

TMP_ROOT = ROOT / ".testtmp"
TMP_ROOT.mkdir(parents=True, exist_ok=True)

@contextlib.contextmanager
def temp_dir():
    path = TMP_ROOT / ("t-" + uuid.uuid4().hex[:10])
    path.mkdir()
    try:
        yield str(path)
    finally:
        shutil.rmtree(str(path), ignore_errors=True)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ARTICLE_ID = "2094715383932573257"
ANSWER_ID = "2024747872659285839"
QUESTION_ID = "1914329673120347774"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
GIF = b"GIF89a" + b"\x00" * 512

def read_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")

def html_response(text: str):
    return 200, {"Content-Type": "text/html; charset=utf-8"}, text.encode("utf-8")

class FakeHttp:
    """替身 HttpClient: 按 URL 片段返回固定响应."""
    def __init__(self, routes=None, fail_urls=(), cookie="z_c0=fake; d_c0=fake"):
        self.routes = routes or {}
        self.fail_urls = tuple(fail_urls)
        self.cookie = cookie
        self.calls = []

    def get(self, url, extra_headers=None, allow_status=()):
        self.calls.append(url)
        for fragment in self.fail_urls:
            if fragment in url:
                raise z.ZhihuError("模拟失败: %s" % url)
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        if "zhimg.com" in url:
            is_gif = url.endswith(".gif")
            return 200, {"Content-Type": "image/gif" if is_gif else "image/png"}, GIF if is_gif else PNG
        raise z.ZhihuError("测试里没有配置这个 URL: %s" % url)

def default_routes():
    return {
        "zhuanlan.zhihu.com/p/" + ARTICLE_ID: html_response(read_fixture("article_page.html")),
        "answer/" + ANSWER_ID: html_response(read_fixture("answer_page.html")),
    }

# 引用
class ParseRefTests(unittest.TestCase):
    def test_article_path(self):
        ref = z.parse_ref("p/2094715383932573257")
        self.assertEqual((ref.kind, ref.target_id), ("article", ARTICLE_ID))
        self.assertEqual(ref.page_url, "https://zhuanlan.zhihu.com/p/" + ARTICLE_ID)

    def test_article_full_url_with_query(self):
        ref = z.parse_ref("https://zhuanlan.zhihu.com/p/2094715383932573257?utm_source=wechat#top")
        self.assertEqual((ref.kind, ref.target_id), ("article", ARTICLE_ID))

    def test_answer_path(self):
        ref = z.parse_ref("question/%s/answer/%s" % (QUESTION_ID, ANSWER_ID))
        self.assertEqual((ref.kind, ref.target_id, ref.question_id), ("answer", ANSWER_ID, QUESTION_ID))
        self.assertEqual(
            ref.page_url,
            "https://www.zhihu.com/question/%s/answer/%s" % (QUESTION_ID, ANSWER_ID),
        )

    def test_answer_full_url(self):
        ref = z.parse_ref("https://www.zhihu.com/question/%s/answer/%s" % (QUESTION_ID, ANSWER_ID))
        self.assertEqual((ref.kind, ref.target_id, ref.question_id), ("answer", ANSWER_ID, QUESTION_ID))

    def test_answer_short_path(self):
        ref = z.parse_ref("answer/2024747872659285839")
        self.assertEqual((ref.kind, ref.target_id), ("answer", ANSWER_ID))

    def test_bare_digits_is_auto(self):
        ref = z.parse_ref("2024747872659285839")
        self.assertEqual((ref.kind, ref.target_id), ("auto", ANSWER_ID))

    def test_whitespace_and_quotes(self):
        ref = z.parse_ref('  "p/2094715383932573257"  ')
        self.assertEqual(ref.target_id, ARTICLE_ID)

    def test_link_zhihu_redirect(self):
        ref = z.parse_ref(
            "https://link.zhihu.com/?target=https%3A%2F%2Fzhuanlan.zhihu.com%2Fp%2F2094715383932573257"
        )
        self.assertEqual((ref.kind, ref.target_id), ("article", ARTICLE_ID))

    def test_invalid(self):
        with self.assertRaises(z.ZhihuError):
            z.parse_ref("随便一段文字")
        with self.assertRaises(z.ZhihuError):
            z.parse_ref("")

# 工具
class UtilsTests(unittest.TestCase):
    def test_safe_filename_strips_illegal_chars(self):
        name = z.safe_filename('a/b\\c:d*e?f"g<h>i|j')
        for ch in '<>:"/\\|?*':
            self.assertNotIn(ch, name)
        self.assertEqual(name, "a b c d e f g h i j")

    def test_safe_filename_length_and_reserved(self):
        self.assertLessEqual(len(z.safe_filename("长" * 200)), 80)
        self.assertEqual(z.safe_filename("CON"), "_CON")
        self.assertEqual(z.safe_filename("   "), "未命名")
        self.assertEqual(z.safe_filename("标题."), "标题")

    def test_asset_slug_is_markdown_safe(self):
        slug = z.asset_slug("Rust (1) 异步 [教程] #2 100%")
        self.assertNotRegex(slug, r"[\s()\[\]#%]")
        self.assertIn("Rust", slug)
        self.assertIn("异步", slug)

    def test_yaml_str_escapes(self):
        self.assertEqual(z.yaml_str('a"b'), '"a\\"b"')
        self.assertEqual(z.yaml_str("a\nb"), '"a b"')

    def test_fmt_time(self):
        self.assertEqual(z.fmt_time(0), "")
        self.assertEqual(z.fmt_time("bad"), "")
        self.assertRegex(z.fmt_time(1700000000), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")
        self.assertRegex(z.fmt_time(1700000000000), r"^\d{4}-")  # 毫秒也能吃

    def test_parse_cookie_text_netscape(self):
        text = (
            "# Netscape HTTP Cookie File\n"
            ".zhihu.com\tTRUE\t/\tTRUE\t1800000000\tz_c0\tsecret\n"
            "#HttpOnly_.zhihu.com\tTRUE\t/\tTRUE\t1800000000\td_c0\tdsecret\n"
        )
        cookie = z.parse_cookie_text(text)
        self.assertIn("z_c0=secret", cookie)
        self.assertIn("d_c0=dsecret", cookie)

    def test_normalize_cookie_string(self):
        self.assertEqual(z.normalize_cookie("Cookie: z_c0=abc; d_c0=xyz;\n"), "z_c0=abc; d_c0=xyz")

    def test_parse_cookie_json(self):
        text = json.dumps([
            {"name": "z_c0", "value": "secret", "domain": ".zhihu.com"},
            {"name": "d_c0", "value": "dsecret"},
        ])
        self.assertEqual(z.parse_cookie_text(text), "z_c0=secret; d_c0=dsecret")

    def test_normalize_cookie_from_curl_bash(self):
        curl = (
            "curl 'https://www.zhihu.com/api/v4/me?include=x' -H 'accept: */*' "
            "-H 'cookie: z_c0=abc123; d_c0=def456; _xsrf=xyz' --compressed"
        )
        self.assertEqual(z.normalize_cookie(curl), "z_c0=abc123; d_c0=def456; _xsrf=xyz")

    def test_normalize_cookie_from_curl_cmd_escaped(self):
        curl = (
            'curl ^"https://www.zhihu.com/api/v4/me^" -H ^"accept: */*^" '
            '-H ^"cookie: z_c0=abc; d_c0=def^"'
        )
        self.assertEqual(z.normalize_cookie(curl), "z_c0=abc; d_c0=def")

    def test_normalize_cookie_from_curl_cookie_flag(self):
        curl = "curl 'https://www.zhihu.com/api/v4/me' -b 'z_c0=abc; d_c0=def'"
        self.assertEqual(z.normalize_cookie(curl), "z_c0=abc; d_c0=def")

    def test_normalize_cookie_from_copied_headers(self):
        headers = (
            "accept: */*\n"
            "accept-language: zh-CN,zh;q=0.9\n"
            "cookie: z_c0=abc; d_c0=def;\n"
            "referer: https://www.zhihu.com/\n"
        )
        self.assertEqual(z.normalize_cookie(headers), "z_c0=abc; d_c0=def")

    def test_normalize_cookie_keeps_plain_value(self):
        self.assertEqual(z.normalize_cookie("z_c0=abc; d_c0=def"), "z_c0=abc; d_c0=def")
        self.assertEqual(z.normalize_cookie("z_c0=abc;"), "z_c0=abc")

    def test_normalize_cookie_from_curl_fragment_without_curl_word(self):
        # 用户只复制了 cURL 的一部分
        fragment = '-H ^"accept: */*^" -H ^"cookie: z_c0=AA; d_c0=BB^" --compressed'
        self.assertEqual(z.normalize_cookie(fragment), "z_c0=AA; d_c0=BB")

    def test_normalize_cookie_from_pairs_table(self):
        table = "z_c0\t2|1:0|10:1700000000|4:z_c0|92:abcdef\n_xsrf\tabc123\n"
        cookie = z.normalize_cookie(table)
        self.assertIn("z_c0=2|1:0|10:1700000000|4:z_c0|92:abcdef", cookie)
        self.assertIn("_xsrf=abc123", cookie)

    def test_normalize_cookie_from_bare_z_c0_value(self):
        token = "2|1:0|10:1700000000|4:z_c0|92:abcdefghijklmnopqrstuvwxyz0123456789"
        self.assertEqual(z.normalize_cookie(token), "z_c0=" + token)

    def test_normalize_cookie_rejects_garbage(self):
        self.assertEqual(z.normalize_cookie("-H ^\"accept: */*^\" --compressed"), "")
        self.assertEqual(z.normalize_cookie("随便一句话"), "")

    def test_validate_cookie(self):
        self.assertTrue(z.validate_cookie(""))
        self.assertTrue(z.validate_cookie("z_c0=short"))
        self.assertTrue(z.validate_cookie("_xsrf=abc"))
        good = "z_c0=" + "x" * 200 + "; d_c0=" + "y" * 40
        self.assertEqual(z.validate_cookie(good), [])
        pairs = z.cookie_pairs(good)
        self.assertEqual(set(pairs), {"z_c0", "d_c0"})

    def test_guess_image_ext(self):
        self.assertEqual(z.guess_image_ext("image/jpeg", "https://a/x"), ".jpg")
        self.assertEqual(z.guess_image_ext("image/webp; charset=x", "https://a/x.jpg"), ".webp")
        self.assertEqual(z.guess_image_ext("", "https://pic1.zhimg.com/v2-abc_r.gif"), ".gif")
        self.assertEqual(z.guess_image_ext("", "https://pic1.zhimg.com/80/v2-abc"), ".jpg")

# HTML 解析
class HtmlParserTests(unittest.TestCase):
    def test_nested_list_stays_nested(self):
        root = z.parse_html("<ul><li>a</li><li>b<ul><li>n1</li></ul></li></ul>")
        outer = root.children[0]
        self.assertEqual([c.tag for c in outer.children], ["li", "li"])
        self.assertEqual([c.tag for c in outer.children[1].children], [None, "ul"])

    def test_unclosed_paragraph_is_closed(self):
        root = z.parse_html("<p>a<p>b<div>c</div>")
        self.assertEqual([c.tag for c in root.children], ["p", "p", "div"])

    def test_skip_script_style_noscript(self):
        root = z.parse_html("<div><p>正文</p><script>x=1</script><style>p{}</style>"
                            "<noscript><img src='a.jpg'/></noscript></div>")
        text = root.get_text()
        self.assertIn("正文", text)
        self.assertNotIn("x=1", text)
        self.assertEqual(root.find_all_tags("img"), [])

    def test_charrefs_decoded(self):
        root = z.parse_html("<p>a &lt; b &amp;&amp; c &gt; d &#20320;</p>")
        self.assertIn("a < b && c > d 你", root.get_text())

    def test_void_and_self_closing(self):
        root = z.parse_html('<p>x<br/>y<img src="1.png"/></p>')
        self.assertEqual([c.tag for c in root.children[0].children], [None, "br", None, "img"])

# 转换器
class RendererTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = z.extract_initial_state(read_fixture("article_page.html"))
        entity = z.find_entity(state, "article", ARTICLE_ID)
        cls.content = entity["content"]
        cls.renderer = z.MarkdownRenderer()
        cls.md = cls.renderer.render(z.parse_html(cls.content))

    def test_code_block_with_language(self):
        self.assertIn("```rust\nasync fn main() {", self.md)
        self.assertIn('let x = 1 < 2;  // "注释"', self.md)
        self.assertIn("```python\ndef add(a, b):", self.md)

    def test_code_block_closing_fence(self):
        self.assertEqual(self.md.count("```"), 4)  # rust / python 各一对

    def test_inline_latex(self):
        self.assertIn("$O(1)$", self.md)
        self.assertIn(r"$\alpha+\beta$", self.md)

    def test_block_latex(self):
        self.assertIn("$$\n\\int_0^1 x^2 \\, dx = \\frac{1}{3}\n$$", self.md)

    def test_table(self):
        self.assertIn("| 语言 | 特点 |", self.md)
        self.assertIn("| --- | --- |", self.md)
        self.assertIn(r"| Rust | 内存安全 \| 无 GC |", self.md)

    def test_nested_list_indent(self):
        self.assertIn("- 第二项`code`\n  - 嵌套一\n  - 嵌套二", self.md)

    def test_ordered_list_start(self):
        self.assertIn("3. 从三开始", self.md)
        self.assertIn("4. 第四项", self.md)

    def test_blockquote(self):
        self.assertIn("> 不要过早优化。", self.md)
        self.assertIn("> —— Donald Knuth", self.md)

    def test_links_are_absolute_and_unwrapped(self):
        self.assertIn("[Async Book](https://rust-lang.org/async-book/)", self.md)
        self.assertIn("[这个回答](https://www.zhihu.com/question/123/answer/456)", self.md)
        self.assertNotIn("link.zhihu.com", self.md)

    def test_headings(self):
        self.assertIn("## 为什么需要异步", self.md)
        self.assertIn("### 一段代码", self.md)

    def test_hard_break(self):
        self.assertIn("状态机。  \n但它并不「免费」", self.md)

    def test_strong_em_del_sup_sub(self):
        self.assertIn("**零成本抽象**", self.md)
        self.assertIn("*强调*", self.md)
        self.assertIn("~~旧内容~~", self.md)
        self.assertIn("H<sub>2</sub>O", self.md)
        self.assertIn("E=mc<sup>2</sup>", self.md)

    def test_hr(self):
        self.assertIn("\n---\n", self.md)

    def test_images_were_collected(self):
        urls = [img.url for img in self.renderer.images]
        self.assertEqual(urls, [
            "https://pic1.zhimg.com/v2-aaa_r.jpg",
            "https://picx.zhimg.com/v2-bbb_r.gif",
        ])
        self.assertEqual(self.md.count(z.IMG_TOKEN_FMT % 0), 1)

    def test_figure_caption(self):
        self.assertIn("*图 1：Future 的状态机*", self.md)

    def test_raw_lt_in_text_is_escaped(self):
        md = z.MarkdownRenderer().render(z.parse_html("<p>if a < b and c > d</p>"))
        self.assertIn("a &lt; b", md)
        self.assertIn("c &gt; d", md)

    def test_inline_code_with_backticks(self):
        md = z.MarkdownRenderer().render(z.parse_html("<p><code>a`b</code></p>"))
        self.assertIn("``a`b``", md)

    def test_pre_with_backticks_bumps_fence(self):
        md = z.MarkdownRenderer().render(z.parse_html("<pre lang='md'><code>a\n```\nb</code></pre>"))
        self.assertIn("````md", md)

    def test_equation_via_span_data_tex(self):
        md = z.MarkdownRenderer().render(z.parse_html('<p><span class="ztext-math" data-tex="x^2"></span></p>'))
        self.assertIn("$x^2$", md)

    def test_line_start_markdown_is_escaped(self):
        md = z.MarkdownRenderer().render(z.parse_html(
            "<p># 不是标题</p><p>- 不是列表</p><p>1. 不是有序列表</p><p>| 不是表格 |</p>"
        ))
        self.assertIn("\\# 不是标题", md)
        self.assertIn("\\- 不是列表", md)
        self.assertIn("1\\. 不是有序列表", md)
        self.assertIn("\\| 不是表格", md)

    def test_h4_and_heading_with_sup(self):
        md = z.MarkdownRenderer().render(z.parse_html("<h4>小标题<sup>1</sup></h4>"))
        self.assertIn("#### 小标题<sup>1</sup>", md)

    def test_image_wrapped_in_link_keeps_link(self):
        renderer = z.MarkdownRenderer()
        md = renderer.render(z.parse_html(
            '<p><a href="https://example.com"><img src="https://pic1.zhimg.com/v2-x.jpg"/></a></p>'
        ))
        self.assertIn("[![](", md)
        self.assertIn("](https://example.com)", md)
        self.assertEqual(len(renderer.images), 1)

    def test_ordered_list_with_multiple_paragraphs(self):
        md = z.MarkdownRenderer().render(z.parse_html(
            "<ol><li><p>第一段</p><p>第二段</p><ul><li>子项</li></ul></li><li>简洁项</li></ol>"
        ))
        self.assertIn("1. 第一段\n\n   第二段", md)
        self.assertIn("   - 子项", md)
        self.assertIn("2. 简洁项", md)

    def test_blockquote_with_list(self):
        md = z.MarkdownRenderer().render(z.parse_html(
            "<blockquote><p>引用：</p><ul><li>项</li></ul></blockquote>"
        ))
        self.assertIn("> 引用：", md)
        self.assertIn("> - 项", md)

# 抓取
class ClientTests(unittest.TestCase):
    def make_client(self, routes=None, fail_urls=()):
        http = FakeHttp(routes if routes is not None else default_routes(), fail_urls=fail_urls)
        return z.ZhihuClient(http), http

    def test_fetch_article(self):
        client, _ = self.make_client()
        doc = client.fetch(z.parse_ref("p/" + ARTICLE_ID))
        self.assertEqual(doc.kind, "article")
        self.assertIn("Rust 异步编程入门", doc.title)
        self.assertEqual(doc.author, "张三")
        self.assertEqual(doc.author_url, "https://www.zhihu.com/people/zhangsan-42")
        self.assertEqual(doc.voteup, "1,234")
        self.assertEqual(doc.comments, "56")
        self.assertRegex(doc.created, r"^\d{4}-\d{2}-\d{2} ")
        self.assertIn("<h2>", doc.content_html)

    def test_fetch_answer(self):
        client, _ = self.make_client()
        doc = client.fetch(z.parse_ref("question/%s/answer/%s" % (QUESTION_ID, ANSWER_ID)))
        self.assertEqual(doc.kind, "answer")
        self.assertEqual(doc.question_title, "如何系统地学习 Rust 异步编程？")
        self.assertEqual(doc.question_id, QUESTION_ID)
        self.assertEqual(doc.author, "李四")
        self.assertIn("how" if False else "简单说", doc.content_html)
        self.assertEqual(
            doc.url, "https://www.zhihu.com/question/%s/answer/%s" % (QUESTION_ID, ANSWER_ID)
        )

    def test_auto_kind_tries_article_then_answer(self):
        # 只配回答页: 请求文章页会失败, 随后应自动按回答重试
        client, _ = self.make_client({"answer/" + ANSWER_ID: html_response(read_fixture("answer_page.html"))})
        doc = client.fetch(z.parse_ref(ANSWER_ID))
        self.assertEqual(doc.kind, "answer")
        self.assertEqual(doc.question_title, "如何系统地学习 Rust 异步编程？")

    def test_dom_fallback_when_no_initial_state(self):
        page = read_fixture("dom_only_page.html")
        client, _ = self.make_client({"answer/" + ANSWER_ID: html_response(page)})
        doc = client.fetch(z.parse_ref("answer/" + ANSWER_ID))
        self.assertIn("DOM 兜底", doc.content_html)
        self.assertEqual(doc.author, "")  # 这份快照没有作者信息
        self.assertTrue(doc.question_title.startswith("没有内嵌数据的回答"))

    def test_failure_message_mentions_cookie(self):
        client, _ = self.make_client(fail_urls=("zhihu.com",))
        with self.assertRaises(z.ZhihuError) as ctx:
            client.fetch(z.parse_ref("p/" + ARTICLE_ID))
        self.assertIn("v4 API", str(ctx.exception))

    def test_missing_entity_gives_clear_error(self):
        page = read_fixture("deleted_page.html")
        client, _ = self.make_client({"/p/": html_response(page)})
        with self.assertRaises(z.ZhihuError) as ctx:
            client.fetch(z.parse_ref("p/" + ARTICLE_ID))
        message = str(ctx.exception)
        self.assertTrue("内嵌数据" in message or "正文" in message)

    def test_page_without_json_falls_back_to_dom(self):
        page = re.sub(r'<script id="js-initialData".*?</script>', "", read_fixture("answer_page.html"), flags=re.S)
        client, _ = self.make_client({"answer/" + ANSWER_ID: html_response(page)})
        doc = client.fetch(z.parse_ref("answer/" + ANSWER_ID))
        self.assertIn("简单说", doc.content_html)
        self.assertEqual(doc.question_title, "如何系统地学习 Rust 异步编程？")

# 保存
class SaverTests(unittest.TestCase):
    def save(self, outdir, ref="p/" + ARTICLE_ID, routes=None, **kwargs):
        http = FakeHttp(routes if routes is not None else default_routes(), cookie="z_c0=x")
        client = z.ZhihuClient(http)
        saver = z.Saver(Path(outdir), http, **kwargs)
        doc = client.fetch(z.parse_ref(ref))
        return saver.save(doc), http

    def test_end_to_end(self):
        with temp_dir() as tmp:
            result, _ = self.save(tmp)
            path = result.path
            self.assertTrue(path.exists())
            self.assertEqual(result.images_ok, 2)
            self.assertEqual(result.images_failed, 0)
            text = path.read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"))
            self.assertIn('zhihu_id: "%s"' % ARTICLE_ID, text)
            self.assertIn("tags:\n  - zhihu\n  - 文章", text)
            self.assertIn("# Rust 异步编程入门", text)
            self.assertNotIn("\ue000", text)

            links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
            self.assertEqual(len(links), 2)
            for link in links:
                self.assertNotIn(" ", link)
                self.assertTrue((Path(tmp) / link).exists(), "图片没落地: %s" % link)
            self.assertTrue(links[0].endswith("img-001.png"))
            self.assertTrue(links[1].endswith("img-002.gif"))

    def test_frontmatter_is_quoted_yaml(self):
        with temp_dir() as tmp:
            result, _ = self.save(tmp)
            lines = result.path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "---")
            end = lines.index("---", 1)
            body = lines[1:end]
            for line in body:
                if line.startswith("  -") or line == "tags:":
                    continue
                key, _, value = line.partition(": ")
                self.assertTrue(value.startswith('"') and value.endswith('"'), "不是合法 YAML: %r" % line)
                json.loads(value)  # 双引号字符串同时也是合法 JSON 字符串
            self.assertIn("created", " ".join(body))

    def test_second_run_is_idempotent(self):
        with temp_dir() as tmp:
            first, _ = self.save(tmp)
            second, _ = self.save(tmp)
            self.assertEqual(first.path, second.path)
            files = [p.name for p in Path(tmp).glob("*.md")]
            self.assertEqual(len(files), 1)

    def test_same_title_different_docs_get_suffix(self):
        with temp_dir() as tmp:
            http = FakeHttp(default_routes())
            client = z.ZhihuClient(http)
            saver = z.Saver(Path(tmp), http, with_images=False)
            doc1 = client.fetch(z.parse_ref("p/" + ARTICLE_ID))
            first = saver.save(doc1)
            doc2 = copy.deepcopy(doc1)
            doc2.target_id = "9999999999"
            doc2.url = "https://zhuanlan.zhihu.com/p/9999999999"
            second = saver.save(doc2)
            self.assertNotEqual(first.path, second.path)
            self.assertIn("(2)", second.path.name)
            self.assertTrue(second.path.exists())
            self.assertEqual(len(list(Path(tmp).glob("*.md"))), 2)

    def test_skip_existing(self):
        with temp_dir() as tmp:
            self.save(tmp)
            with self.assertRaises(z.Skipped):
                self.save(tmp, skip_existing=True)

    def test_no_images_keeps_remote_urls(self):
        with temp_dir() as tmp:
            result, _ = self.save(tmp, with_images=False)
            text = result.path.read_text(encoding="utf-8")
            self.assertIn("https://pic1.zhimg.com/v2-aaa_r.jpg", text)
            self.assertEqual(list(Path(tmp).glob("assets")), [])

    def test_image_failure_falls_back_to_url(self):
        with temp_dir() as tmp:
            http = FakeHttp(default_routes(), fail_urls=("pic1.zhimg.com", "picx.zhimg.com"))
            client = z.ZhihuClient(http)
            doc = client.fetch(z.parse_ref("p/" + ARTICLE_ID))
            result = z.Saver(Path(tmp), http).save(doc)
            text = result.path.read_text(encoding="utf-8")
            self.assertEqual(result.images_failed, 2)
            self.assertIn("https://pic1.zhimg.com/v2-aaa_r.jpg", text)
            self.assertEqual(list(Path(tmp).glob("*.md")).__len__(), 1)

    def test_data_uri_image(self):
        with temp_dir() as tmp:
            content = ('<p>图：</p><p><img src="data:image/png;base64,'
                       'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="/></p>')
            state = {"initialState": {"entities": {"articles": {ARTICLE_ID: {
                "id": ARTICLE_ID, "title": "内联图片", "content": content,
                "author": {"name": "王五", "url_token": "wangwu"},
            }}}}}
            page = '<html><body><script id="js-initialData" type="text/json">%s</script></body></html>' % (
                json.dumps(state, ensure_ascii=False))
            http = FakeHttp({"zhuanlan.zhihu.com/p/": html_response(page)})
            doc = z.ZhihuClient(http).fetch(z.parse_ref("p/" + ARTICLE_ID))
            result = z.Saver(Path(tmp), http).save(doc)
            text = result.path.read_text(encoding="utf-8")
            links = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
            self.assertEqual(len(links), 1)
            self.assertTrue((Path(tmp) / links[0]).exists())
            self.assertEqual(result.images_ok, 1)

    def test_answer_file_name_uses_question_and_author(self):
        with temp_dir() as tmp:
            result, _ = self.save(tmp, ref="question/%s/answer/%s" % (QUESTION_ID, ANSWER_ID))
            self.assertEqual(result.path.stem, "如何系统地学习 Rust 异步编程？ - 李四")

    def test_custom_name_format(self):
        with temp_dir() as tmp:
            result, _ = self.save(tmp, name_format_article="{id}-{author}")
            self.assertEqual(result.path.stem, "%s-张三" % ARTICLE_ID)

# 命令行
class CliTests(unittest.TestCase):
    def test_collect_refs_from_args_and_file(self):
        parser = z.build_parser()
        with temp_dir() as tmp:
            path = Path(tmp) / "links.txt"
            path.write_text("# 注释\np/111111\n\nquestion/222/answer/333\np/111111\n", encoding="utf-8")
            args = parser.parse_args(["answer/999", "-f", str(path)])
            refs, bad = z._collect_refs(args)
        self.assertEqual(
            [(r.kind, r.target_id) for r in refs],
            [("answer", "999"), ("article", "111111"), ("answer", "333")],
        )
        self.assertEqual(bad, [])

    def test_collect_refs_from_pasted_text(self):
        parser = z.build_parser()
        blob = (
            '一些说明文字 <a href="https://zhuanlan.zhihu.com/p/444444">文章</a>\n'
            '  "https://www.zhihu.com/question/555/answer/666"  \n'
            "完全无关的一行\n"
        )
        with temp_dir() as tmp:
            path = Path(tmp) / "links.txt"
            path.write_text(blob, encoding="utf-8")
            refs, bad = z._collect_refs(parser.parse_args(["-f", str(path)]))
        self.assertEqual(
            [(r.kind, r.target_id) for r in refs],
            [("article", "444444"), ("answer", "666")],
        )
        self.assertEqual(len(bad), 1)

    def test_main_returns_2_without_refs(self):
        with temp_dir() as tmp:
            self.assertEqual(z.main(["-o", tmp]), 2)
            self.assertEqual(z.main(["-o", tmp, "这不是编号"]), 2)

    def test_main_writes_file(self):
        original = z.HttpClient
        z.HttpClient = lambda **kwargs: FakeHttp(default_routes())  # type: ignore[assignment]
        try:
            with temp_dir() as tmp:
                code = z.main(["-o", tmp, "p/" + ARTICLE_ID, "--no-images", "--delay", "0"])
                self.assertEqual(code, 0)
                files = list(Path(tmp).glob("*.md"))
                self.assertEqual(len(files), 1)
                self.assertIn("零成本抽象", files[0].read_text(encoding="utf-8"))
        finally:
            z.HttpClient = original  # type: ignore[assignment]

# 公式的 URL 编码还原
sys.path.insert(0, str(ROOT / "tools"))
import fix_formula_plus as repair  # noqa: E402

EQUATION_URL = "https://www.zhihu.com/equation?tex="

def form_encode(tex: str) -> str:
    """模拟知乎的 form-urlencoded：空格 -> '+'，字面加号 -> '%2B'。"""
    return urllib.parse.quote(tex, safe="").replace("%20", "+")

class FormulaEncodingTests(unittest.TestCase):
    def render_img(self, src: str, attrs: str = 'eeimg="2"') -> str:
        html = '<p><img class="eeimg" %s src="%s"/></p>' % (attrs, src)
        return z.MarkdownRenderer().render(z.parse_html(html))

    def test_form_encoded_spaces_restored(self):
        tex = r"\frac{1}{G} R + \frac{1}{g^2} F^2"
        md = self.render_img(EQUATION_URL + form_encode(tex))
        self.assertIn(r"\frac{1}{G} R + \frac{1}{g^2} F^2", md)
        self.assertNotIn("++", md)

    def test_plus_runs_between_operands_kept_as_operator(self):
        # 作者写 `R + \frac`, 若字面加号未转义, 编码后是连续的 3 个 '+'(1.1.1)
        self.assertEqual(
            z._expand_plus_runs(r"\frac{1}{G} R+++\frac{1}{g^2}"),
            r"\frac{1}{G} R + \frac{1}{g^2}",
        )
        self.assertEqual(z._expand_plus_runs("a++b"), "a + b")
        # 行首缩进的连续 '+' 是空格, 不是加号
        self.assertEqual(z._expand_plus_runs("\\\\++++++++S"), "\\\\        S")

    def test_literal_plus_not_eaten(self):
        md = self.render_img(EQUATION_URL + form_encode(r"\alpha+\beta"), 'eeimg="1"')
        self.assertIn(r"$\alpha+\beta$", md)

    def test_structure_plus_becomes_space(self):
        tex = r"\displaystyle\mathrm{d} \mathbf{B} = \frac{I\mathrm{d}\mathbf{l}\times \mathbf{r}}{\mathbf{r}^3}"
        md = self.render_img(EQUATION_URL + form_encode(tex))
        self.assertIn(r"\mathrm{d} \mathbf{B} = \frac", md)
        self.assertNotIn("+", md.replace(r"\+", ""))

    def test_align_star_converted_to_aligned(self):
        tex = r"\begin{align*} S &= \int x \\ Z &= 1 \end{align*}"
        md = self.render_img(EQUATION_URL + form_encode(tex))
        self.assertIn(r"\begin{aligned}", md)
        self.assertIn(r"\end{aligned}", md)
        self.assertNotIn("align*", md)

    def test_trailing_double_backslash_trimmed(self):
        tex = r"\begin{align*} a &= b \\ \end{align*} \\"
        md = self.render_img(EQUATION_URL + form_encode(tex))
        body = md.split("$$")[1].strip()
        self.assertFalse(body.endswith("\\\\"))

    def test_alt_latex_preferred(self):
        alt = r"\displaystyle\mathrm{d} \mathbf{B} = \frac{1}{r^3}"
        raw = form_encode(r"\displaystyle\mathrm{d} \mathbf{B} = \frac{1}{r^3}")
        html = ('<p><img class="eeimg" eeimg="2" alt="%s" '
                'src="%s%s"/></p>') % (alt.replace('"', "&quot;"), EQUATION_URL, raw)
        md = z.MarkdownRenderer().render(z.parse_html(html))
        self.assertIn(r"\mathrm{d} \mathbf{B} = \frac{1}{r^3}", md)

    def test_plain_text_plus_untouched(self):
        # 普通公式 (不是 URL 编码的) 不该被动
        md = self.render_img(EQUATION_URL + "a%2Bb", 'eeimg="1"')
        self.assertIn("$a+b$", md)

    # 1.1.2 新增: 结尾 \\ 以及短公式
    def test_inline_trailing_double_backslash_removed(self):
        # 结尾的 \\ 会把闭合 $ 转义掉, 必须去掉 (1.1.2)
        md = self.render_img(EQUATION_URL + "x%3D1%5C%5C", 'eeimg="1"')
        self.assertIn("$x=1$", md)
        self.assertNotIn("\\\\$", md)

    def test_formula_with_internal_linebreak_promoted_to_display(self):
        # 行内 $...$ 不能放换行符, 含 \\ 时必须升级为整段 (1.1.2)
        md = self.render_img(EQUATION_URL + "a%3D1%5C%5Cb%3D2", 'eeimg="1"')
        self.assertIn("$$", md)

    def test_short_formula_plus_suspicion(self):
        # 短公式 `\delta+S+=+0` 只有 2 个强证据, 旧阈值 3 会漏 (1.1.2)
        self.assertTrue(z.plus_repair_suspicion(r"\delta+S+=+0"))
        # 正常写法不能被误判
        self.assertFalse(z.plus_repair_suspicion(r"a+b"))
        self.assertFalse(z.plus_repair_suspicion(r"(a)+b"))
        self.assertFalse(z.plus_repair_suspicion(r"x = y + z"))

class RepairToolTests(unittest.TestCase):
    def test_repair_block_inline_and_keep_code_fence(self):
        broken = (
            "# 标题\n\n"
            "$$\n++\\begin{align*}+++++S+&=+\\int+\\mathrm{d}x+++\\frac{1}{G}+R\\end{align*}\n$$\n\n"
            "行内：$\\nabla+\\cdot+\\mathbf{B}+=+0$\n\n"
            "```\n$$ a+b $$\n```\n"
        )
        fixed, count = repair.repair_text(broken)
        self.assertGreaterEqual(count, 2)
        self.assertNotIn("++++", fixed)
        self.assertIn(r"S &= \int \mathrm{d}x + \frac{1}{G} R", fixed.replace("  ", " "))
        self.assertIn(r"\nabla \cdot \mathbf{B} = 0", fixed)
        self.assertIn("$$ a+b $$", fixed)  # 代码块里的内容不动

    def test_repair_is_idempotent(self):
        broken = "$$\n\\mathrm{d}+\\mathbf{B}+=+\\frac{1}{r}\n$$\n"
        once, count1 = repair.repair_text(broken)
        self.assertGreaterEqual(count1, 1)
        twice, count2 = repair.repair_text(once)
        self.assertEqual(count2, 0)
        self.assertEqual(once, twice)

    def test_repair_leaves_clean_formula_alone(self):
        clean = "$$\n\\frac{a}{b} + \\frac{c}{d}\n$$\n"
        fixed, count = repair.repair_text(clean)
        self.assertEqual(count, 0)
        self.assertEqual(clean, fixed)

    def test_repair_trailing_backslash_keeps_real_plus(self):
        # 只删结尾 \\, 不能把 a+b 的加号吃掉 (1.1.2)
        broken = "行内 $a+b\\\\$ 结束\n"
        fixed, count = repair.repair_text(broken)
        self.assertEqual(count, 1)
        self.assertIn("$a+b$", fixed)

    def test_repair_short_inline_plus(self):
        # 短公式 += 漏修问题 (1.1.2)
        broken = "经典物理遵循 $\\delta+S+=+0$ 原理\n"
        fixed, count = repair.repair_text(broken)
        self.assertEqual(count, 1)
        self.assertIn(r"$\delta S = 0$", fixed)

    def test_repair_inline_with_linebreak_becomes_display(self):
        # 行内公式含 \\ 时自动改成整段 (1.1.2)
        broken = "张量 $\\mathbf{T} = a \\\\ b$ 结束\n"
        fixed, count = repair.repair_text(broken)
        self.assertEqual(count, 1)
        self.assertIn("$$\n", fixed)
        self.assertNotIn("$\\mathbf{T} = a \\\\ b$", fixed)

    def test_repair_trailing_backslash_idempotent(self):
        broken = "$\\nabla \\cdot \\mathbf{B} = 0\\\\$\n"
        once, count1 = repair.repair_text(broken)
        self.assertEqual(count1, 1)
        twice, count2 = repair.repair_text(once)
        self.assertEqual(count2, 0)
        self.assertEqual(once, twice)

class MathCommandTests(unittest.TestCase):
    """1.1.3 新增: 字体命令归一化 + 外层定界符剥离."""
    def norm(self, tex: str) -> str:
        return z.normalize_math_commands(tex)

    def test_bold_to_mathbf(self):
        self.assertEqual(self.norm(r"\bold CP"), r"\mathbf{CP}")
        self.assertEqual(self.norm(r"\bold{Citation}"), r"\mathbf{Citation}")

    def test_bm_to_boldsymbol(self):
        self.assertEqual(self.norm(r"\bm x"), r"\boldsymbol{x}")
        self.assertEqual(self.norm(r"\bm{g}"), r"\boldsymbol{g}")

    def test_scr_to_mathscr(self):
        self.assertEqual(self.norm(r"\scr H"), r"\mathscr{H}")

    def test_legacy_switches_mapped(self):
        self.assertEqual(self.norm(r"{\bf x}"), r"{\mathbf{x}}")
        self.assertEqual(self.norm(r"\Bbb R"), r"\mathbb{R}")

    def test_switch_applies_to_rest_of_group(self):
        # \rm 是开关, 作用于本组剩余内容, 不能只包下一个 token (1.1.3 回归)
        src = r"{\rm \kern{1em} MID\_SHORT\kern{1em}}"
        self.assertEqual(self.norm(src), r"{\mathrm{\kern{1em} MID\_SHORT\kern{1em}}}")

    def test_macro_with_command_argument(self):
        # 参数型宏后面跟控制序列时, 要连它的括号参数一起包 (1.1.3 回归)
        self.assertEqual(self.norm(r"\bold \kern{1em}"), r"\mathbf{\kern{1em}}")

    def test_kern_argument_never_orphaned(self):
        src = r"{\rm \kern{4em} LONG\kern{4em}}"
        out = self.norm(src)
        self.assertNotIn(r"\kern}", out)
        self.assertIn(r"\mathrm{\kern{4em} LONG\kern{4em}}", out)

    def test_scope_stops_at_table_structure(self):
        # 开关不能把 \cr \hline & \\ 吞进字体参数 (1.1.3 回归)
        self.assertEqual(self.norm(r"{\it 5\kern{9pt}\cr\hline}"),
                         r"{\mathit{5\kern{9pt}}\cr\hline}")
        self.assertEqual(self.norm(r"\rm a & \rm b"), r"\mathrm{a}& \mathrm{b}")

    def test_nested_switch_inside_wrapped_content(self):
        # 开关包住的内容里还有 \rm, 必须递归处理 (1.1.3 回归)
        self.assertEqual(self.norm(r"\scr O_{\rm I}"), r"\mathscr{O_{\mathrm{I}}}")

    def test_cjk_inside_tag_group_is_math(self):
        # \tag{中文} 里的中文不应导致"这不是公式"的误判
        self.assertTrue(repair.looks_like_math(r"\bf \color{Lavender}{\tag*{佐蔵　瑾}}"))

    def test_display_delimiters_stripped(self):
        out = self.norm(r"\[g=\text{diag}\left( 1,-1,-1,-1 \right)\]")
        self.assertNotIn(r"\[", out)
        self.assertNotIn(r"\]", out)
        self.assertIn(r"\text{diag}", out)

    def test_real_brackets_untouched(self):
        tex = r"\left[ x \right] + a[1] + b\\[2pt] c"
        self.assertEqual(self.norm(tex), tex)

    def test_supported_commands_untouched(self):
        tex = r"\mathbf{A}+\boldsymbol{B}+x^2"
        self.assertEqual(self.norm(tex), tex)

class ReferenceTests(unittest.TestCase):
    """1.1.3 新增: 参考文献角标 -> [N] + 文末 ## 参考."""
    def render(self, html: str) -> str:
        return z.MarkdownRenderer().render(z.parse_html(html))

    def test_sup_citation_becomes_bracket(self):
        md = self.render("<p>应该<sup>[1]</sup>定义成能量密度。</p>")
        self.assertIn("应该[1]定义成", md)
        self.assertNotIn("<sup>", md)

    def test_normal_superscript_kept(self):
        md = self.render("<p>E=mc<sup>2</sup></p>")
        self.assertIn("<sup>2</sup>", md)

    def test_reference_urls_appended(self):
        md = self.render('<p>引用<sup data-url="https://example.com/a">[1]</sup>。</p>')
        self.assertIn("[1]", md)
        self.assertIn("## 参考", md)
        self.assertIn("- [1] https://example.com/a", md)

    def test_existing_entries_get_heading(self):
        md = self.render("<p>正文<sup>[1]</sup>。</p><p>[1] 作者. 标题.</p>")
        self.assertIn("## 参考", md)
        self.assertIn("[1] 作者. 标题.", md)

    def test_no_citation_no_section(self):
        md = self.render("<p>没有引用。</p>")
        self.assertNotIn("## 参考", md)

    def test_repair_bold_and_bracket_delims(self):
        # 1.1.3: \bold -> \mathbf, \[ \] 外层定界符去掉
        broken = "公式 $\\bold CP$ 和 $\\[g=\\text{diag}(1,-1)\\]$ 结束\n"
        fixed, count = repair.repair_text(broken)
        self.assertEqual(count, 2)
        self.assertIn(r"$\mathbf{CP}$", fixed)
        self.assertIn(r"$g=\text{diag}(1,-1)$", fixed)

    def test_repair_keeps_left_bracket(self):
        clean = "$\\left[ x \\right] + a[1]$\n"
        fixed, count = repair.repair_text(clean)
        self.assertEqual(count, 0)
        self.assertEqual(clean, fixed)

if __name__ == "__main__":
    unittest.main(verbosity=2)