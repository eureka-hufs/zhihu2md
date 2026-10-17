#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成测试用的知乎页面快照（模拟真实页面的结构）。

真实页面里正文放在 <script id="js-initialData" type="text/json"> 的
initialState.entities 里，正文本身是一段 HTML 字符串。这里用同样的结构造样例，
这样即使没有网络也能把「解析 + 转换 + 落盘」整条链路跑通。

运行：python tests/make_fixtures.py
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures"

ARTICLE_ID = "2094715383932573257"
ANSWER_ID = "2024747872659285839"
QUESTION_ID = "1914329673120347774"

EQUATION_SRC = (
    "https://www.zhihu.com/equation?tex=%5Cint_0%5E1%20x%5E2%20%5C%2C%20dx%20%3D%20%5Cfrac%7B1%7D%7B3%7D"
)

ARTICLE_CONTENT = """
<h2>为什么需要异步</h2>
<p>在 Rust 里，异步是一种<strong>零成本抽象</strong>：编译器把 <code>async fn</code> 展开成状态机。<br>但它并不「免费」，你得先理解 <code>Future</code> 和 <code>Pin</code>。例如判断 <code>argc &lt; 3</code> 这种写法在文中出现时不能被当成 HTML。</p>
<p>行内公式：时间复杂度 <img class="eeimg" eeimg="1" src="https://www.zhihu.com/equation?tex=O(1)" alt="O(1)"/>，符号 <img class="eeimg" eeimg="1" src="https://www.zhihu.com/equation?tex=%5Calpha%2B%5Cbeta" alt="\\alpha+\\beta"/> 也不该丢。</p>
<p>参考 <a href="https://link.zhihu.com/?target=https%3A%2F%2Frust-lang.org%2Fasync-book%2F" target="_blank">Async Book</a>，或者看 <a href="/question/123/answer/456">这个回答</a>。</p>
<blockquote><p>不要过早优化。<br>—— Donald Knuth</p></blockquote>
<figure data-size="normal"><noscript><img src="https://pic1.zhimg.com/v2-aaa_r.jpg" data-original="https://pic1.zhimg.com/v2-aaa_r.jpg"/></noscript><img src="data:image/svg+xml;utf8,&lt;svg/&gt;" data-rawwidth="1200" data-rawheight="800" data-original="https://pic1.zhimg.com/v2-aaa_r.jpg" class="origin_image zh-lightbox-thumb lazy" width="1200"/><figcaption>图 1：Future 的状态机</figcaption></figure>
<p><img src="https://picx.zhimg.com/v2-bbb_720w.jpg" data-original="https://picx.zhimg.com/v2-bbb_r.gif" alt="动图演示" data-caption="动图演示"/></p>
<h3>一段代码</h3>
<pre lang="rust"><code><span class="k">async</span> <span class="k">fn</span> <span class="nf">main</span>() {
    <span class="kd">let</span> x = <span class="mi">1</span> &lt; <span class="mi">2</span>;  <span class="c1">// &quot;注释&quot;</span>
    println!(<span class="s">"{x}"</span>);
}</code></pre>
<div class="highlight"><pre lang="python"><code><span class="k">def</span> <span class="nf">add</span>(a, b):
    <span class="k">return</span> a + b</code></pre></div>
<p>然后是块级公式：</p>
<p><img class="eeimg" eeimg="2" src="__EQUATION__" alt="\\int_0^1 x^2 \\, dx = \\frac{1}{3}"/></p>
<h3>列表与表格</h3>
<ul><li>第一项</li><li>第二项<code>code</code><ul><li>嵌套一</li><li>嵌套二</li></ul></li></ul>
<ol start="3"><li>从三开始</li><li>第四项</li></ol>
<table><thead><tr><th>语言</th><th>特点</th></tr></thead><tbody><tr><td>Rust</td><td>内存安全 | 无 GC</td></tr><tr><td>C++</td><td>自由</td></tr></tbody></table>
<hr>
<p>上下标：H<sub>2</sub>O，E=mc<sup>2</sup>，删除线 <del>旧内容</del>，斜体 <em>强调</em>。</p>
""".replace("__EQUATION__", EQUATION_SRC)

ANSWER_CONTENT = """
<p>简单说，<strong>先读官方文档</strong>，然后动手写。</p>
<p>我整理了三步：</p>
<ol><li>把 <code>Future</code> 当成一个状态机来理解</li><li>搞清楚 <code>poll</code> 的唤醒机制</li><li>最后再碰 <code>Pin</code></li></ol>
<p><img src="https://pic3.zhimg.com/v2-ccc_720w.jpg" data-original="https://pic3.zhimg.com/v2-ccc_r.jpg" alt="学习路线"/></p>
<pre lang="cpp"><code><span class="kt">int</span> <span class="n">main</span>() {
    <span class="k">return</span> <span class="mi">0</span>;
}</code></pre>
<p>更多细节见 <a href="https://zhuanlan.zhihu.com/p/2094715383932573257">我的专栏</a>。</p>
"""

DOM_ONLY_CONTENT = """
<p>这一篇用 DOM 兜底解析。</p>
<p>正文里有一张图：</p>
<p><img src="https://pic2.zhimg.com/v2-ddd.jpg" data-original="https://pic2.zhimg.com/v2-ddd.jpg" alt="dom 图"/></p>
<pre lang="bash"><code>pip install nothing</code></pre>
"""


def build_article_page() -> str:
    entity = {
        "id": ARTICLE_ID,
        "type": "article",
        "title": "Rust 异步编程入门：从 Future 到 Pin",
        "content": ARTICLE_CONTENT.strip(),
        "author": {"id": "u-100", "name": "张三", "url_token": "zhangsan-42"},
        "created": 1700000000,
        "updated": 1700003600,
        "voteup_count": 1234,
        "comment_count": 56,
    }
    state = {
        "initialState": {
            "entities": {
                "articles": {ARTICLE_ID: entity},
                "users": {"u-100": {"name": "张三", "url_token": "zhangsan-42"}},
            },
        }
    }
    payload = json.dumps(state, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>Rust 异步编程入门：从 Future 到 Pin - 知乎</title>
<meta property="og:title" content="Rust 异步编程入门：从 Future 到 Pin"/>
<meta name="author" content="张三"/>
</head>
<body>
<div id="root">
  <h1 class="Post-Title">Rust 异步编程入门：从 Future 到 Pin</h1>
  <div class="Post-RichTextContainer">
    <div class="RichText ztext Post-RichText css-1g0fqss" options="[object Object]">{ARTICLE_CONTENT.strip()}</div>
  </div>
</div>
<script id="js-initialData" type="text/json">{payload}</script>
</body>
</html>
"""


def build_answer_page() -> str:
    entity = {
        "id": ANSWER_ID,
        "type": "answer",
        "content": ANSWER_CONTENT.strip(),
        "question": {"id": QUESTION_ID, "title": "如何系统地学习 Rust 异步编程？"},
        "author": {"id": "u-200", "name": "李四", "url_token": "lisi-7"},
        "created_time": 1690000000,
        "updated_time": 1690005000,
        "voteup_count": 4321,
        "comment_count": 88,
    }
    state = {
        "initialState": {
            "entities": {
                "answers": {ANSWER_ID: entity},
                "questions": {QUESTION_ID: {"id": QUESTION_ID, "title": "如何系统地学习 Rust 异步编程？"}},
                "users": {"u-200": {"name": "李四", "url_token": "lisi-7"}},
            },
        }
    }
    payload = json.dumps(state, ensure_ascii=False)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>如何系统地学习 Rust 异步编程？ - 知乎</title>
</head>
<body>
<div id="root">
  <h1 class="QuestionHeader-title">如何系统地学习 Rust 异步编程？</h1>
  <div class="AnswerItem">
    <div class="RichText ztext" options="[object Object]">{ANSWER_CONTENT.strip()}</div>
  </div>
</div>
<script id="js-initialData" type="text/json">{payload}</script>
</body>
</html>
"""


def build_dom_only_page() -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>没有内嵌数据的回答 - 知乎</title>
</head>
<body>
<div class="RichText ztext Post-RichText">{DOM_ONLY_CONTENT.strip()}</div>
</body>
</html>
"""


def build_deleted_page() -> str:
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"/><title>知乎 - 你似乎来到了没有知识存在的荒原</title></head>
<body><p>你似乎来到了没有知识存在的荒原</p></body></html>
"""


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    files = {
        "article_page.html": build_article_page(),
        "answer_page.html": build_answer_page(),
        "dom_only_page.html": build_dom_only_page(),
        "deleted_page.html": build_deleted_page(),
    }
    for name, text in files.items():
        path = FIXTURES / name
        path.write_text(text, encoding="utf-8", newline="\n")
        print("wrote %s (%d bytes)" % (path, len(text)))


if __name__ == "__main__":
    main()
