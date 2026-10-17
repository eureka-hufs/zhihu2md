#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""zhihu2md 的离线测试：不联网，用真实的页面结构快照跑通整条链路。

运行：
    python -m unittest discover -s tests -v
    python tests/test_zhihu2md.py
"""

from __future__ import annotations

import contextlib
import copy
import json
import re
import shutil
import sys
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import zhihu2md as z  # noqa: E402

# 工作区内的临时目录（系统 temp 目录在沙箱里可能不可写）
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
    """替身 HttpClient：按 URL 片段返回固定响应。"""

    def __init__(self, routes=None, fail_urls=(), cookie="z_c0=fake; d_c0=fake"):
        self.routes = routes or {}
        self.fail_urls = tuple(fail_urls)
        self.cookie = cookie
        self.calls = []

    def get(self, url, extra_headers=None, allow_status=()):
        self.calls.append(url)
        for fragment in self.fail_urls:
            if fragment in url:
                raise z.ZhihuError("模拟失败：%s" % url)
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        if "zhimg.com" in url:
            is_gif = url.endswith(".gif")
            return 200, {"Content-Type": "image/gif" if is_gif else "image/png"}, GIF if is_gif else PNG
        raise z.ZhihuError("测试里没有配置这个 URL：%s" % url)


def default_routes():
    return {
        "zhuanlan.zhihu.com/p/" + ARTICLE_ID: html_response(read_fixture("article_page.html")),
        "answer/" + ANSWER_ID: html_response(read_fixture("answer_page.html")),
    }


# --------------------------------------------------------------- 引用 -----

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


# --------------------------------------------------------------- 工具 -----

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
        # 用户只复制了 cURL 的一部分（真实踩过的坑）
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


# ---------------------------------------------------------- HTML 解析 -----

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


# ------------------------------------------------------------ 转换器 -----

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


# -------------------------------------------------------------- 抓取 -----

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
        # 只配回答页：请求文章页会失败，随后应自动按回答重试
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


# -------------------------------------------------------------- 保存 -----

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
                self.assertTrue((Path(tmp) / link).exists(), "图片没落地：%s" % link)
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
                self.assertTrue(value.startswith('"') and value.endswith('"'), "不是合法 YAML：%r" % line)
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


# -------------------------------------------------------------- 命令行 -----

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
