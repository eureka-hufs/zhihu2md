#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zhihu2md.py -- 把知乎专栏文章 / 回答保存成本地 Markdown (Obsidian 友好)
提供一个知乎编号, 得到:
  1. 抓取正文 (优先解析页面内嵌的 initialState JSON, 必要时回退到 v4 API / 直接解析 DOM)
  2. 把正文 HTML 转换成 Markdown: 标题、段落、引用、列表、表格、代码块、LaTeX 公式
  3. 把正文里的图片全部下载到本地 assets 目录, 并把链接改写成相对路径
  4. 生成带 YAML front matter 的 .md 文件，可以直接丢进 Obsidian 库

只依赖 Python 标准库 (3.8+).
"""

from __future__ import annotations
import argparse
import base64
import datetime
import gzip
import hashlib
import html as _html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

__version__ = "1.1.2"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
ZHIHU_HOME = "https://www.zhihu.com/"

_VERBOSE = False
def _setup_stdout() -> None:
    """Windows 控制台遇到生僻字时不要直接崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

def log(msg: str) -> None:
    print(msg, flush=True)

def debug(msg: str) -> None:
    if _VERBOSE:
        print("  · " + msg, flush=True)

def warn(msg: str) -> None:
    print("  ! " + msg, file=sys.stderr, flush=True)

class ZhihuError(RuntimeError):
    """抓取/解析失败, 消息可直接展示给用户."""

class Skipped(ZhihuError):
    """目标文件已存在, 按 --skip-existing 跳过."""

_ILLEGAL_WIN = '<>:"/\\|?*'
_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}

def safe_filename(name: str, max_len: int = 80) -> str:
    """把任意标题变成 Windows / macOS / Obsidian 都能用的文件名."""
    s = unicodedata.normalize("NFC", str(name or ""))
    s = re.sub(r"[\x00-\x1f\x7f\u200b-\u200f\ufeff]", " ", s)
    s = s.replace("\u2026", "...")
    for ch in _ILLEGAL_WIN:
        s = s.replace(ch, " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip(" .")  # Windows 不允许结尾是空格或点
    if len(s) > max_len:
        s = s[:max_len].rstrip(" .")
    if not s:
        s = "未命名"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s

# 图片目录名会被写进 Markdown 链接的括号里, 必须避开 Markdown / URL 的敏感字符
_ASSET_BAD_RE = re.compile(r"[\s()（）\[\]［］{}｛｝#＃%％^`'\"<>|?？*:/\\]+")

def asset_slug(name: str, max_len: int = 60) -> str:
    """把标题变成适合放进 Markdown 链接里的目录名 (无空格、无括号)."""
    s = safe_filename(name, max_len)
    s = _ASSET_BAD_RE.sub("_", s)
    s = re.sub(r"_{2,}", "_", s).strip(" ._-")
    return s or "assets"

def yaml_str(value: object) -> str:
    """生成 YAML 双引号标量."""
    s = str(value if value is not None else "")
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    s = re.sub(r"[\r\n\t]+", " ", s)
    return '"%s"' % s

def fmt_time(ts: object) -> str:
    """知乎的 created / updated 是秒级时间戳, 转成本地时间字符串."""
    try:
        n = int(ts)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n > 10_000_000_000:  # 毫秒
        n //= 1000
    try:
        return datetime.datetime.fromtimestamp(n).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""

_WS_RE = re.compile(r"[ \t\r\n\u00a0\u200b]+")

def squash_ws(text: str) -> str:
    return _WS_RE.sub(" ", text.replace("\u200b", ""))

# 极简 HTML 树
VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
}

# 这些标签的内部内容对正文没有意义，直接整段跳过
SKIP_TAGS = {"script", "style", "noscript", "svg", "template", "iframe", "object"}

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "caption", "dd", "div",
    "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "html", "li", "main",
    "nav", "ol", "p", "pre", "section", "table", "tbody", "td", "tfoot", "th",
    "thead", "tr", "ul", "video", "audio", "details", "summary", "center",
}

INLINE_TAGS = {
    "a", "abbr", "b", "bdi", "bdo", "big", "br", "cite", "code", "data", "del",
    "dfn", "em", "font", "i", "img", "ins", "kbd", "label", "mark", "q", "rb",
    "rp", "rt", "ruby", "s", "samp", "small", "span", "strike", "strong",
    "sub", "sup", "time", "tt", "u", "var", "wbr", "math",
}

# 遇到这些标签时, 隐式关闭同名的上一个标签
AUTOCLOSE = {
    "li": {"li"},
    "p": {"p"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "tr": {"tr", "td", "th"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "option": {"option"},
    "thead": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tbody": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tfoot": {"thead", "tbody", "tfoot", "tr", "td", "th"},
}

_RAWTEXT_TAGS = {"script", "style", "noscript", "template"}

class Node:
    """HTML 树节点. tag 为 None 时表示纯文本节点。"""
    __slots__ = ("tag", "attrs", "children", "text")
    def __init__(self, tag: Optional[str], attrs: Optional[Dict[str, str]] = None, text: str = ""):
        self.tag = tag
        self.attrs: Dict[str, str] = attrs or {}
        self.children: List["Node"] = []
        self.text = text

    # 常用查询
    @property
    def classes(self) -> List[str]:
        return (self.attrs.get("class") or "").split()

    def has_class(self, name: str) -> bool:
        return name in self.classes

    def class_contains(self, fragment: str) -> bool:
        return any(fragment in c for c in self.classes)

    def get(self, key: str, default: str = "") -> str:
        return self.attrs.get(key, default) or default

    def append(self, child: "Node") -> None:
        self.children.append(child)

    def iter(self) -> Iterator["Node"]:
        """先序遍历所有后代节点 (含文本节点)."""
        for child in self.children:
            yield child
            yield from child.iter()

    def find(self, pred) -> Optional["Node"]:
        for child in self.children:
            if pred(child):
                return child
            got = child.find(pred)
            if got is not None:
                return got
        return None

    def find_all(self, pred, out: Optional[List["Node"]] = None) -> List["Node"]:
        out = [] if out is None else out
        for child in self.children:
            if pred(child):
                out.append(child)
            child.find_all(pred, out)
        return out

    def find_tag(self, *tags: str) -> Optional["Node"]:
        return self.find(lambda n: n.tag in tags)

    def find_all_tags(self, *tags: str) -> List["Node"]:
        return self.find_all(lambda n: n.tag in tags)

    def get_text(self) -> str:
        """取纯文本"""
        if self.tag is None:
            return self.text
        pieces = []
        for child in self.children:
            if child.tag == "br":
                pieces.append("\n")
            elif child.tag is None:
                pieces.append(child.text)
            elif child.tag == "img":
                alt = child.get("alt")
                if alt:
                    pieces.append(alt)
            else:
                pieces.append(child.get_text())
        return "".join(pieces)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        if self.tag is None:
            return "Text(%r)" % self.text[:40]
        return "Node(%s, %r, %d children)" % (self.tag, self.classes, len(self.children))

class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: List[Node] = [self.root]
        self._skip: List[str] = []  # 正在跳过的标签栈

    # 内部
    def _open_index(self, names: Iterable[str]) -> int:
        names = set(names) if not isinstance(names, set) else names
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag in names:
                return i
        return -1

    def _pop_to(self, index: int) -> None:
        if index > 0:
            del self.stack[index:]

    def _node(self) -> Node:
        return self.stack[-1]

    # HTMLParser 回调
    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.append(tag)
            return
        if tag in SKIP_TAGS:
            if tag not in VOID_TAGS:
                self._skip.append(tag)
            return

        attr_map: Dict[str, str] = {}
        for key, value in attrs:
            key = key.lower()
            if key not in attr_map:  # 重复属性保留第一个
                attr_map[key] = value if value is not None else ""

        if tag in AUTOCLOSE and self.stack[-1].tag in AUTOCLOSE[tag]:
            self.stack.pop()

        if tag in BLOCK_TAGS:
            # <div>/<figure>/... 出现在未闭合的 <p> 里时, 先把 <p> 收掉
            if self.stack[-1].tag == "p":
                self.stack.pop()

        node = Node(tag, attr_map)
        self._node().append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)
        tag = tag.lower()
        if tag not in VOID_TAGS:
            # <foo /> 形式的自闭合: 把自己弹掉
            if len(self.stack) > 1 and self.stack[-1].tag == tag:
                self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip:
            if tag == self._skip[-1]:
                self._skip.pop()
            return
        if tag in VOID_TAGS:
            return
        idx = self._open_index({tag})
        if idx > 0:
            self._pop_to(idx)

    def handle_data(self, data: str) -> None:
        if self._skip or not data:
            return
        parent = self._node()
        if parent.tag in _RAWTEXT_TAGS:
            return
        if parent.children and parent.children[-1].tag is None:
            parent.children[-1].text += data
        else:
            parent.append(Node(None, text=data))

    def handle_comment(self, data: str) -> None:
        return

    def handle_decl(self, decl: str) -> None:
        return

    def handle_pi(self, data: str) -> None:
        return

    def unknown_decl(self, data: str) -> None:
        return

def parse_html(text: str) -> Node:
    builder = _TreeBuilder()
    try:
        builder.feed(text or "")
        builder.close()
    except Exception:  # 解析器在极端畸形 HTML 下可能抛错, 保留已解析部分
        pass
    return builder.root

# 引用解析
@dataclass
class Ref:
    kind: str                      # "article" | "answer"
    target_id: str                 # 文章 id 或回答 id
    question_id: str = ""          # 仅回答
    raw: str = ""

    @property
    def page_url(self) -> str:
        if self.kind == "article":
            return "https://zhuanlan.zhihu.com/p/%s" % self.target_id
        qid = self.question_id or "0"
        return "https://www.zhihu.com/question/%s/answer/%s" % (qid, self.target_id)

    @property
    def canonical_url(self) -> str:
        if self.kind == "article":
            return "https://zhuanlan.zhihu.com/p/%s" % self.target_id
        if self.question_id:
            return "https://www.zhihu.com/question/%s/answer/%s" % (self.question_id, self.target_id)
        return "https://www.zhihu.com/answer/%s" % self.target_id


_ARTICLE_RE = re.compile(r"(?:^|/)p/(\d{1,20})")
_QA_RE = re.compile(r"(?:^|/)question/(\d{1,20})/answer/(\d{1,20})")
_ANSWER_RE = re.compile(r"(?:^|/)answer/(\d{1,20})")
_DIGITS_RE = re.compile(r"^\d{5,}$")
_DOMAIN_RE = re.compile(r"^(?:[a-z0-9-]+\.)*zhihu\.com/", re.I)
_LINK_TARGET_RE = re.compile(r"[?&]target=([^&]+)")


def _normalize_ref_text(raw: str) -> str:
    s = (raw or "").strip().strip("\"'<>()[]\u200b \t\r\n")
    s = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", "", s)  # 去掉 https://
    s = re.sub(r"^//", "", s)
    s = s.split("#")[0]
    s = s.split("?")[0]
    s = _DOMAIN_RE.sub("", s, count=1)  # 去掉 zhuanlan.zhihu.com/ 之类的域名
    return s.strip("/")

def parse_ref(text: str) -> Ref:
    """把用户给的编号 / 链接解析成 Ref.
    支持: p/123、question/1/answer/2、answer/2、纯数字,
    以及对应的完整链接 (含 link.zhihu.com 跳转链接).
    """
    raw = (text or "").strip()
    if not raw:
        raise ZhihuError("编号为空")

    target = _LINK_TARGET_RE.search(raw)
    if target:
        inner = urllib.parse.unquote(target.group(1))
        if inner and inner != raw:
            return parse_ref(inner)

    s = _normalize_ref_text(raw)

    m = _QA_RE.search(s)
    if m:
        return Ref("answer", m.group(2), m.group(1), raw=raw)
    m = _ARTICLE_RE.search(s)
    if m:
        return Ref("article", m.group(1), raw=raw)
    m = _ANSWER_RE.search(s)
    if m:
        return Ref("answer", m.group(1), "", raw=raw)
    if _DIGITS_RE.match(s):
        # 只有一个数字: 分不清文章还是回答, 交给抓取层依次尝试
        return Ref("auto", s, raw=raw)

    raise ZhihuError(
        "看不懂这个编号: %r\n"
        "  支持: p/2094715383932573257、question/1914329673120347774/answer/2024747872659285839、\n"
        "        answer/2024747872659285839, 或者完整链接" % raw
    )

# 页面数据提取
_SCRIPT_INITIAL = re.compile(
    r"<script[^>]*\bid=[\"']js-initialData[\"'][^>]*>(.*?)</script>", re.S | re.I
)
_SCRIPT_STATE = re.compile(
    r"window\.(?:__INITIAL_STATE__|__INITDATA__|initialState)\s*=\s*(\{.*?\})\s*;?\s*</script>",
    re.S,
)
_SCRIPT_INITIAL_ALT = re.compile(
    r"<script[^>]*\btype=[\"']application/json[\"'][^>]*\bid=[\"']js-initialData[\"'][^>]*>(.*?)</script>",
    re.S | re.I,
)
# 页面结构变了: 在这些锚点后面做括号配对, 把 JSON 抠出来
_JSON_ANCHORS = ('id="js-initialData"', "id='js-initialData'", "initialState")

def _json_loads(raw: str) -> Optional[dict]:
    raw = (raw or "").strip()
    if not raw:
        return None
    for candidate in (raw, html_unescape_once(raw)):
        try:
            data = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            return data
    return None

def html_unescape_once(text: str) -> str:
    return _html.unescape(text)

def _json_object_at(text: str, brace_start: int) -> Optional[dict]:
    """从 text[brace_start] 这个 '{' 开始做引号/转义感知的括号配对."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(brace_start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return _json_loads(text[brace_start:index + 1])
    return None

def extract_initial_state(page_html: str) -> Optional[dict]:
    page_html = page_html or ""
    for pattern in (_SCRIPT_INITIAL, _SCRIPT_INITIAL_ALT, _SCRIPT_STATE):
        m = pattern.search(page_html)
        if not m:
            continue
        data = _json_loads(m.group(1))
        if data:
            debug("命中内嵌 JSON (%d 字节)" % len(m.group(1)))
            return data

    for anchor in _JSON_ANCHORS:
        pos = page_html.find(anchor)
        if pos < 0:
            continue
        brace = page_html.find("{", pos)
        if brace < 0:
            continue
        data = _json_object_at(page_html, brace)
        if data:
            debug("命中内嵌 JSON (括号配对, 锚点 %r)" % anchor)
            return data
    return None

def iter_dicts(obj: object) -> Iterator[dict]:
    """深度优先遍历 JSON 里所有 dict."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)

def find_entity(state: dict, kind: str, target_id: str) -> Optional[dict]:
    """在 initialState 里找出 id 对应的文章 / 回答对象."""
    target_id = str(target_id)
    initial = state.get("initialState") if isinstance(state.get("initialState"), dict) else state
    entities = initial.get("entities") if isinstance(initial, dict) else None

    if isinstance(entities, dict):
        buckets = []
        if kind in ("article", "auto"):
            buckets.append(entities.get("articles"))
        if kind in ("answer", "auto"):
            buckets.append(entities.get("answers"))
        for bucket in buckets:
            if isinstance(bucket, dict) and isinstance(bucket.get(target_id), dict):
                return bucket[target_id]

    # 兜底: 全树搜索「id 对得上且带 content」
    best: Optional[dict] = None
    for item in iter_dicts(state):
        if str(item.get("id", "")) != target_id:
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if kind == "answer" and "question" not in item:
            continue
        if kind == "article" and "title" not in item:
            continue
        if best is None or len(content) > len(best.get("content") or ""):
            best = item
    return best

def resolve_author(obj: dict, state: dict) -> Tuple[str, str]:
    """返回 (作者名, 作者主页链接)."""
    author = obj.get("author")
    name = ""
    token = ""
    if isinstance(author, dict):
        name = str(author.get("name") or "")
        token = str(author.get("url_token") or author.get("id") or "")
    elif isinstance(author, str) and author:
        name = author
    if not name:
        name = str(obj.get("author_name") or "")

    if not name:
        # 从 entities.users 里补
        initial = state.get("initialState") if isinstance(state.get("initialState"), dict) else state
        entities = initial.get("entities") if isinstance(initial, dict) else None
        users = entities.get("users") if isinstance(entities, dict) else None
        uid = ""
        if isinstance(author, dict):
            uid = str(author.get("id") or "")
        if isinstance(users, dict) and uid and isinstance(users.get(uid), dict):
            name = str(users[uid].get("name") or "")
            token = str(users[uid].get("url_token") or uid)

    url = ""
    if token and not token.startswith("http"):
        url = "https://www.zhihu.com/people/%s" % token
    return name.strip(), url

def resolve_question(obj: dict, state: dict) -> Tuple[str, str]:
    question = obj.get("question")
    title = ""
    qid = ""
    if isinstance(question, dict):
        title = str(question.get("title") or "")
        qid = str(question.get("id") or "")
    initial = state.get("initialState") if isinstance(state.get("initialState"), dict) else state
    entities = initial.get("entities") if isinstance(initial, dict) else None
    questions = entities.get("questions") if isinstance(entities, dict) else None
    if isinstance(questions, dict) and qid and isinstance(questions.get(qid), dict):
        title = title or str(questions[qid].get("title") or "")
    return title.strip(), qid

@dataclass
class Document:
    kind: str = "article"
    target_id: str = ""
    title: str = ""
    author: str = ""
    author_url: str = ""
    url: str = ""
    content_html: str = ""
    question_title: str = ""
    question_id: str = ""
    created: str = ""
    updated: str = ""
    voteup: str = ""
    comments: str = ""
    source_note: str = ""

    @property
    def display_title(self) -> str:
        if self.kind == "answer" and self.question_title:
            return self.question_title
        return self.title or "知乎内容 %s" % self.target_id

# HTTP 层
class HttpClient:
    def __init__(
        self,
        cookie: str = "",
        timeout: float = 30.0,
        retries: int = 3,
        proxy: str = "",
    ) -> None:
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.proxy = proxy.strip()

    def _opener(self) -> urllib.request.OpenerDirector:
        handlers: List[urllib.request.BaseHandler] = []
        if self.proxy:
            handlers.append(urllib.request.ProxyHandler({"http": self.proxy, "https": self.proxy}))
        handlers.append(urllib.request.HTTPRedirectHandler())
        return urllib.request.build_opener(*handlers)

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        headers = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept-Encoding": "gzip, deflate",
            "Referer": ZHIHU_HOME,
            "Connection": "close",
        }
        if self.cookie:
            headers["Cookie"] = self.cookie
        if extra:
            headers.update(extra)
        return headers

    def get(
        self,
        url: str,
        extra_headers: Optional[Dict[str, str]] = None,
        allow_status: Sequence[int] = (),
    ) -> Tuple[int, Dict[str, str], bytes]:
        """GET 一个 URL, 返回 (状态码, 响应头, 响应体). 失败会重试."""
        opener = self._opener()
        last_err: Optional[BaseException] = None
        for attempt in range(1, self.retries + 1):
            req = urllib.request.Request(url, headers=self._headers(extra_headers))
            try:
                with opener.open(req, timeout=self.timeout) as resp:
                    data = resp.read()
                    status = getattr(resp, "status", None) or resp.getcode() or 200
                    headers = dict(resp.headers.items())
                return int(status), headers, self._decode_body(data, headers)
            except urllib.error.HTTPError as exc:
                body = b""
                try:
                    body = exc.read() or b""
                except Exception:
                    pass
                headers = dict(exc.headers.items()) if exc.headers else {}
                body = self._decode_body(body, headers) if body else b""
                if exc.code in allow_status:
                    return exc.code, headers, body
                if exc.code in (403, 404, 410, 401) or 400 <= exc.code < 500 and exc.code != 429:
                    reason = self._explain_status(exc.code, body)
                    raise ZhihuError(reason) from None
                last_err = exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                last_err = exc
            except Exception as exc:  # pragma: no cover - 兜底
                last_err = exc
            if attempt < self.retries:
                delay = 1.5 * attempt
                debug("请求失败 (%s), %.1fs 后重试 %d/%d" % (last_err, delay, attempt, self.retries))
                time.sleep(delay)
        raise ZhihuError("网络请求失败: %s\n  URL: %s" % (last_err, url))

    @staticmethod
    def _decode_body(data: bytes, headers: Dict[str, str]) -> bytes:
        encoding = ""
        for key, value in headers.items():
            if key.lower() == "content-encoding":
                encoding = (value or "").lower()
                break
        try:
            if "gzip" in encoding:
                return gzip.decompress(data)
            if "deflate" in encoding:
                try:
                    return zlib.decompress(data)
                except zlib.error:
                    return zlib.decompress(data, -zlib.MAX_WBITS)
        except Exception:
            return data
        return data

    @staticmethod
    def _explain_status(status: int, body: bytes) -> str:
        head = body[:400].decode("utf-8", "replace")
        if status in (404, 410):
            return "知乎返回 %d: 内容不存在, 可能已被删除或链接有误." % status
        if status == 401:
            return "知乎返回 401: Cookie 已失效, 请重新登录后复制 Cookie."
        if status == 403:
            hint = (
                "知乎返回 403: 需要登录态或触发了反爬验证.\n"
                "  处理办法: 浏览器登录知乎 → F12 → Network 里任选一个 www.zhihu.com 请求, \n"
                "           复制请求头 Cookie 的完整值 → 用 --cookie 或 --cookie-file 传进来.\n"
                "  如果已经带了 Cookie 仍然 403, 通常是触发了验证码: 用浏览器打开一次该页面, \n"
                "           做完验证后再试; 或者降低抓取频率."
            )
            if "login" in head.lower() or "signin" in head.lower():
                hint += "\n  （响应看起来是登录页, 说明 Cookie 缺失或已过期.）"
            return hint
        return "知乎返回 %d：%s" % (status, head[:200])

def decode_text(data: bytes, headers: Dict[str, str]) -> str:
    charset = ""
    for key, value in headers.items():
        if key.lower() == "content-type" and "charset=" in (value or "").lower():
            charset = value.lower().split("charset=")[-1].split(";")[0].strip()
            break
    for enc in ([charset] if charset else []) + ["utf-8", "gb18030"]:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")

# HTML → MD
_HEADING_START = re.compile(r"^(#{1,6})(\s)")
_BULLET_START = re.compile(r"^([-+*])(\s)")
_ORDERED_START = re.compile(r"^(\d{1,9})([.)])(\s)")

def escape_line_start(line: str) -> str:
    """正文行首如果长得像 Markdown 语法, 转义掉真正的语法字符.
    注意 `1. xxx` 要转义成 `1\\. xxx` (背斜杠加在数字前面无效, 会把反斜杠原样留下).
    """
    stripped = line.lstrip()
    if not stripped:
        return line
    indent = line[: len(line) - len(stripped)]
    if _HEADING_START.match(stripped) or _BULLET_START.match(stripped):
        return indent + "\\" + stripped
    ordered = _ORDERED_START.match(stripped)
    if ordered:
        return indent + ordered.group(1) + "\\" + stripped[len(ordered.group(1)):]
    if stripped.startswith("|") or stripped.startswith("~~~"):
        return indent + "\\" + stripped
    return line
_BACKTICK_RE = re.compile(r"`+")

@dataclass
class ImageRef:
    """正文里的一张图片, 还没下载."""
    url: str
    alt: str = ""
    data_uri: bool = False

IMG_TOKEN_FMT = "\ue000ZIMG%d\ue001"
IMG_TOKEN_RE = re.compile("\ue000ZIMG(\\d+)\ue001")
_LANG_ALIASES = {
    "js": "javascript",
    "ts": "typescript",
    "py": "python",
    "py3": "python",
    "python3": "python",
    "c++": "cpp",
    "cplusplus": "cpp",
    "cs": "csharp",
    "sh": "bash",
    "shell": "bash",
    "zsh": "bash",
    "console": "bash",
    "yml": "yaml",
    "text": "",
    "plain": "",
    "plaintext": "",
    "none": "",
    "default": "",
    "matlab": "matlab",
}

def normalize_lang(lang: str) -> str:
    lang = (lang or "").strip().strip(".").lower()
    if not lang:
        return ""
    lang = re.split(r"[\s,;]", lang)[0]
    return _LANG_ALIASES.get(lang, lang)

def _first(*values: Optional[str]) -> str:
    for value in values:
        if value and value.strip():
            return value.strip()
    return ""

# 1.1.1 update. 解决公式转换输出大量 + 号异常
# 异常原因: 知乎 equation?tex= 用的是 form-urlencoded, 旧代码用 urllib.parse.unquote, space 变 + 号...
_TEX_RAW_RE = re.compile(r"[?&]tex=([^&#]*)")
_PLUS_RUN_RE = re.compile(r"\+\+")
_PLUS_CMD_RE = re.compile(r"\+\\[A-Za-z]")
_PLUS_STRUCT_RE = re.compile(r"[=&{}\[\]()]\+|\+[=&{}\[\]()]")
# 1.1.2 update: 更严格的"强证据" —— 加号与结构符号**紧贴**才计数
# (已经修好的 ` + \frac` 两侧有空格, 不会命中). 两个方向分开数,
# 否则一个 `+=` 会把后面的 `=+` 一起吃掉, 导致短公式漏判.
_STRONG_PLUS_BEFORE_RE = re.compile(r"[=&{}()\[\],;:]\+")
_STRONG_PLUS_AFTER_RE = re.compile(r"\+[=&{}()\[\],;:]")
# 加号紧贴关系符/尖括号: 正常数学里极少见, 命中即可判定为编码残留
_PLUS_RELATION_RE = re.compile(r"\+[=&<>]|[=&<>]\+")
_ALIGN_BEGIN_RE = re.compile(r"\\begin\{(?:align\*?|eqnarray\*?|gather\*?|split)\}")
_ALIGN_END_RE = re.compile(r"\\end\{(?:align\*?|eqnarray\*?|gather\*?|split)\}")

def plus_encoding_evidence(text: str) -> int:
    r"""判断一段公式文本是否被"空格 -> '+'"编码过: 证据越多越像.
    只看**紧贴**结构符号的加号 (`+\int`、`=+`、`+[`) 和连续加号,
    所以已经修好的 ` + \frac`(两侧有空格的真加号) 不会被误判.
    """
    text = text or ""
    return (3 * len(_PLUS_RUN_RE.findall(text))
            + len(_PLUS_CMD_RE.findall(text))
            + len(_PLUS_STRUCT_RE.findall(text)))

def strong_plus_count(text: str) -> int:
    """数"加号紧贴结构符号"的次数 (两个方向分别统计, 不会互相吃掉)."""
    text = text or ""
    return (len(_STRONG_PLUS_BEFORE_RE.findall(text))
            + len(_STRONG_PLUS_AFTER_RE.findall(text)))

def plus_repair_suspicion(text: str) -> bool:
    """判断公式里的 '+' 是否可能是"空格被编码"的结果.

    三级判断, 从强到弱:
      1. 出现 `++`: 连续加号几乎不可能是运算;
      2. 出现 `+=`、`=+`、`+<` 这类紧贴关系符/尖括号的组合: 正常数学里极少见;
      3. 加号紧贴括号/花括号之处 >= 2.
    正常的 `a+b`、`(a)+b`、`x = +b` 都不会命中.
    """
    text = text or ""
    if "+" not in text:
        return False
    if "++" in text:
        return True
    if _PLUS_RELATION_RE.search(text):
        return True
    if strong_plus_count(text) >= 2:
        return True
    return plus_encoding_evidence(text) >= 3

def _expand_plus_runs(text: str) -> str:
    """把 form-encoded 的 '+' 还原成空格, 同时保留真正的加号运算符.
    知乎常见的编码是"空格 -> '+'", 而真正的加号有时也是裸 '+', 光看字面分不清.
    经验规则 (1.1.1 update):
      * 夹在两个"操作数"之间的 2~3 个连续 '+'  -> 一个加号运算符  ' + '
      * 其余情况（挨着 & = { } ( ) [ ] \\ 等结构符号、或出现在行首缩进）-> 空格
    """
    out: List[str] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "+":
            out.append(text[i])
            i += 1
            continue
        j = i
        while j < n and text[j] == "+":
            j += 1
        run = j - i
        left = text[i - 1] if i > 0 else ""
        right = text[j] if j < n else ""
        left_operand = bool(left) and (left.isalnum() or left in "})]")
        right_operand = bool(right) and (right.isalnum() or right in "{([\\")
        if 2 <= run <= 3 and left_operand and right_operand:
            out.append(" + ")
        else:
            out.append(" " * run)
        i = j
    return "".join(out)

def decode_tex_param(raw: str) -> str:
    """解码知乎公式的 tex 参数.
    知乎有两种编码方式, 必须区分, 否则公式会坏:
      * form-urlencoded: 空格 -> '+'，字面加号 -> '%2B'（本参数常见这种）
      * encodeURIComponent: 空格 -> '%20'，字面加号保持 '+'
    直接 unquote 会把空格变成一堆字面加号; 直接 unquote_plus 又会吃掉
    `a+b` 里的加号, 所以按证据判断, 并在必要时按"连续加号"还原空格与运算符.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    if "%2b" in raw.lower():
        # 有被转义的字面加号 -> 裸 '+' 只可能是空格
        return urllib.parse.unquote(raw.replace("+", " "))

    plain = urllib.parse.unquote(raw)
    if "%20" in raw.lower() or " " in plain:
        return plain
    if "+" not in plain:
        return plain

    evidence = plus_encoding_evidence(plain)
    # 1.1.2 update: 短公式(如 `\delta+S+=+0`) 证据只有 1~2 个, 旧阈值 3 会漏修,
    # 改用 plus_repair_suspicion 做三级判定; 长公式仍按 evidence >= 3 兜底.
    if plus_repair_suspicion(plain) or evidence >= 3:
        return urllib.parse.unquote(_expand_plus_runs(raw))
    return plain

def looks_like_latex(text: str) -> bool:
    """粗略判断一段文本是不是 LaTeX 源码 (而不是渲染后的普通文字)."""
    text = text or ""
    return "\\" in text and ("{" in text or "^" in text or "_" in text)

def strip_trailing_breaks(tex: str) -> str:
    r"""1.1.2 新增: 去掉公式结尾的 `\` 或 `\\`.
    知乎 tex 参数(尤其整段公式)常以换行符 `\\` 结尾; 留在 `$...$` 里会把
    闭合的 `$` 转义掉 -> Obsidian 整条公式渲染失败, 显示成原始文本.
    行内公式和整段公式都要处理, 所以单独抽出来.
    """
    tex = (tex or "").strip()
    while tex:
        stripped = re.sub(r"[\\\s]+$", "", tex).strip()
        if stripped == tex:
            break
        tex = stripped
    return tex

def clean_math(tex: str, display: bool) -> str:
    """合并多余空白; 整段公式里的 align* 等换成 MathJax/Obsidian 更稳的 aligned."""
    tex = (tex or "").replace("\u00a0", " ").replace("\r", " ").replace("\n", " ")
    tex = re.sub(r"[ \t]{2,}", " ", tex).strip()
    # 1.1.2 update: 结尾的 \ / \\ 行内公式也会残留, 统一在这里去掉
    tex = strip_trailing_breaks(tex)
    if display:
        tex = _ALIGN_BEGIN_RE.sub(lambda _m: "\\begin{aligned}", tex)
        tex = _ALIGN_END_RE.sub(lambda _m: "\\end{aligned}", tex)
    return tex

class MarkdownRenderer:
    """把知乎的正文 HTML 转成 Obsidian / CommonMark 友好的 Markdown."""
    def __init__(self, base_url: str = "") -> None:
        self.base_url = base_url or ZHIHU_HOME
        self.images: List[ImageRef] = []
        self.warnings: List[str] = []

    # 对外入口
    def render(self, node: Node) -> str:
        blocks = self._blocks(node)
        text = "\n\n".join(b for b in blocks if b.strip())
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip() + "\n"

    # 块级
    def _blocks(self, node: Node) -> List[str]:
        out: List[str] = []
        buf: List[str] = []

        def flush() -> None:
            if buf:
                para = self._para("".join(buf))
                if para.strip():
                    out.append(para)
                buf.clear()

        for child in node.children:
            if child.tag is None or child.tag in INLINE_TAGS:
                piece = self._inline(child)
                if piece:
                    buf.append(piece)
            else:
                flush()
                out.extend(self._block(child))
        flush()
        return out

    def _block(self, node: Node) -> List[str]:
        tag = node.tag
        if tag == "p":
            return [self._para(self._inline_children(node))]
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            return [self._heading(node, int(tag[1]))]
        if tag in ("ul", "ol"):
            return [self._list(node, 0)]
        if tag == "blockquote":
            return [self._blockquote(node)]
        if tag == "pre":
            return [self._code_block(node)]
        if tag == "figure":
            return self._figure(node)
        if tag == "img":
            piece = self._image(node)
            return [piece] if piece else []
        if tag == "table":
            return [self._table(node)]
        if tag == "hr":
            return ["---"]
        if tag in ("br", "wbr", "noscript", "script", "style"):
            return []
        if tag in ("video", "audio", "iframe"):
            return self._media(node)
        if tag in ("div", "section", "article", "main", "header", "footer", "aside",
                   "center", "details", "summary", "form", "fieldset", "dl", "dt", "dd",
                   "body", "html", "#document", "tbody", "thead", "tfoot", "tr",
                   "td", "th", "li", "caption"):
            return self._blocks(node)
        return self._blocks(node)

    @staticmethod
    def _para(text: str) -> str:
        if not text:
            return ""
        text = text.replace("\u200b", "").replace("\ufeff", "")
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        lines = [ln.rstrip("\u3000") for ln in text.split("\n")]
        # 段首如果恰好像 Markdown 语法，转义掉，避免正文被误解析
        text = "\n".join(escape_line_start(line) for line in lines)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip("\n")

    def _heading(self, node: Node, level: int) -> str:
        text = self._inline_children(node).strip()
        text = re.sub(r"[ \t]+", " ", text).strip().rstrip("#").strip()
        text = text.replace("\n", " ")
        if not text:
            return ""
        return "#" * min(level, 6) + " " + text

    # 列表
    def _list(self, node: Node, indent: int) -> str:
        ordered = node.tag == "ol"
        try:
            number = int(node.get("start") or 1)
        except ValueError:
            number = 1
        pad = " " * indent
        lines: List[str] = []
        for li in [c for c in node.children if c.tag == "li"]:
            marker = ("%d. " % number) if ordered else "- "
            number += 1
            cont = " " * (indent + len(marker))
            parts, nested = self._list_item(li, indent + len(marker))
            body = "\n\n".join(p for p in parts if p.strip())
            body_lines = body.split("\n") if body else [""]
            if body_lines[0].strip():
                out = [pad + marker + body_lines[0]]
            else:
                out = [pad + marker.rstrip()]
            for line in body_lines[1:]:
                out.append((cont + line) if line.strip() else "")
            out.extend(nested)
            lines.append("\n".join(out))
        return "\n".join(lines)

    def _list_item(self, li: Node, nested_indent: int) -> Tuple[List[str], List[str]]:
        parts: List[str] = []
        nested: List[str] = []
        buf: List[str] = []

        def flush() -> None:
            if buf:
                para = self._para("".join(buf))
                if para.strip():
                    parts.append(para)
                buf.clear()

        for child in li.children:
            if child.tag in ("ul", "ol"):
                flush()
                nested.extend(self._list(child, nested_indent).split("\n"))
            elif child.tag is None or child.tag in INLINE_TAGS:
                piece = self._inline(child)
                if piece:
                    buf.append(piece)
            else:
                flush()
                parts.extend(b for b in self._block(child) if b.strip())
        flush()
        return parts, nested

    def _blockquote(self, node: Node) -> str:
        inner = "\n\n".join(b for b in self._blocks(node) if b.strip())
        if not inner.strip():
            return ""
        out = []
        for line in inner.split("\n"):
            out.append(("> " + line).rstrip() if line.strip() else ">")
        return "\n".join(out)

    # 代码块
    def _code_block(self, node: Node) -> str:
        code_node = node.find_tag("code") or node
        lang = normalize_lang(_first(node.get("lang"), code_node.get("lang")))
        if not lang:
            for cls in list(node.classes) + list(code_node.classes):
                m = re.match(r"(?:language-|lang-|highlight-)([\w+#.-]+)", cls, re.I)
                if m:
                    lang = normalize_lang(m.group(1))
                    break
        text = self._code_text(code_node)
        longest = max([len(m.group(0)) for m in _BACKTICK_RE.finditer(text)] or [0])
        fence = "`" * max(3, longest + 1)
        return "%s%s\n%s\n%s" % (fence, lang, text, fence)

    def _code_text(self, node: Node) -> str:
        pieces: List[str] = []

        def walk(cur: Node) -> None:
            for child in cur.children:
                if child.tag is None:
                    pieces.append(child.text)
                elif child.tag == "br":
                    pieces.append("\n")
                else:
                    walk(child)

        walk(node)
        text = "".join(pieces)
        if "\n" not in text:
            # 有的渲染器把每一行包在 <span class="line"> 里
            line_nodes = [n for n in node.find_all(lambda x: x.tag is not None and x.class_contains("line"))]
            if len(line_nodes) >= 2:
                text = "\n".join(n.get_text() for n in line_nodes)
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").replace("\u200b", "")
        lines = [ln.rstrip() for ln in text.split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines)

    # 图片 / 公式
    def _figure(self, node: Node) -> List[str]:
        out: List[str] = []
        for img in node.find_all_tags("img"):
            piece = self._image(img)
            if piece:
                out.append(piece)
        for cap in node.find_all_tags("figcaption"):
            text = self._inline_children(cap).strip().replace("\n", " ")
            if text:
                out.append("*%s*" % text)
        return out

    def _image(self, node: Node) -> str:
        special = self._equation(node)
        if special is not None:
            return special

        src = _first(
            node.get("data-original"),
            node.get("data-actualsrc"),
            node.get("data-src"),
            node.get("data-thumbnail"),
            node.get("src"),
        )
        if not src:
            return ""
        alt = _first(node.get("alt"), node.get("data-caption"))
        alt = re.sub(r"\s+", " ", alt).replace("[", "\\[").replace("]", "\\]")

        if src.startswith("data:"):
            self.images.append(ImageRef(src, alt, data_uri=True))
        elif src.startswith("//"):
            self.images.append(ImageRef("https:" + src, alt))
        elif src.startswith("http"):
            self.images.append(ImageRef(src, alt))
        else:
            self.warnings.append("跳过无法识别的图片地址: %s" % src[:120])
            return ""
        return "![%s](%s)" % (alt, IMG_TOKEN_FMT % (len(self.images) - 1))

    def _equation(self, node: Node) -> Optional[str]:
        """知乎的公式是 <img src=".../equation?tex=...">, 还原成 LaTeX."""
        src = node.get("src", "")
        if "equation" not in src and "data-tex" not in node.attrs:
            return None
        tex = ""
        attr_tex = _first(node.get("data-tex"), node.get("data-formula"))
        alt = _first(node.get("alt"))
        if attr_tex:
            tex = urllib.parse.unquote(attr_tex)
        elif looks_like_latex(alt):
            # alt 里存的是作者原文的 LaTeX，不经过 URL 编码，空格最可靠
            tex = urllib.parse.unquote(alt)
        else:
            m = _TEX_RAW_RE.search(src)
            if m:
                tex = decode_tex_param(m.group(1))
        if not tex and alt:
            tex = urllib.parse.unquote(alt)
        if not tex:
            return None
        # 1.1.2 update: 先去掉结尾的 \ 或 \\, 再判断"是否整段公式".
        # 顺序反了的话 `x=1\\` 会被当成"含换行的整段公式"而变成 $$ 块.
        tex = strip_trailing_breaks(tex)
        display = node.get("eeimg") == "2" or "\\begin{" in tex or "\\displaystyle" in tex
        if "\\\\" in tex:
            # 1.1.2 update: 带换行符的公式放进行内 $...$ 会渲染失败, 改当整段公式
            display = True
        tex = clean_math(tex, display)
        if not tex:
            return None
        if display:
            return "\n$$\n%s\n$$\n" % tex
        return "$%s$" % tex

    def _media(self, node: Node) -> List[str]:
        src = _first(node.get("src"), node.get("data-src"), node.get("href"))
        if not src:
            return []
        src = self._abs_url(src)
        return ["[媒体文件](%s)" % src]

    # 表格
    def _table(self, node: Node) -> str:
        rows: List[Node] = []

        def collect(cur: Node) -> None:
            for child in cur.children:
                if child.tag == "tr":
                    rows.append(child)
                elif child.tag in ("thead", "tbody", "tfoot"):
                    collect(child)

        collect(node)
        grid: List[List[str]] = []
        for tr in rows:
            cells = []
            for cell in tr.children:
                if cell.tag in ("td", "th"):
                    text = self._inline_children(cell)
                    text = re.sub(r"[ \t]*\n[ \t]*", "<br>", text.strip())
                    text = text.replace("|", "\\|")
                    cells.append(text)
            if cells:
                grid.append(cells)
        if not grid:
            return ""
        width = max(len(r) for r in grid)
        for row in grid:
            row.extend([""] * (width - len(row)))
        header = grid[0]
        body = grid[1:]
        lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * width) + " |"]
        for row in body:
            lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    # 行内
    def _inline_children(self, node: Node) -> str:
        return "".join(self._inline(child) for child in node.children)

    def _inline(self, node: Node) -> str:
        tag = node.tag
        if tag is None:
            return self._escape_text(squash_ws(node.text))
        if tag == "br":
            return "  \n"
        if tag == "img":
            return self._image(node)
        if tag in ("script", "style", "noscript", "svg", "template"):
            return ""
        if tag in ("strong", "b"):
            inner = self._inline_children(node)
            return "**%s**" % inner if inner.strip() else ""
        if tag in ("em", "i"):
            inner = self._inline_children(node)
            return "*%s*" % inner if inner.strip() else ""
        if tag in ("del", "s", "strike"):
            inner = self._inline_children(node)
            return "~~%s~~" % inner if inner.strip() else ""
        if tag == "code":
            return self._inline_code(node.get_text())
        if tag == "a":
            return self._anchor(node)
        if tag == "span":
            special = self._equation(node)
            if special is not None:
                return special
            return self._inline_children(node)
        if tag in ("sup", "sub", "u", "mark", "kbd", "samp", "var", "q"):
            inner = self._inline_children(node)
            if not inner.strip():
                return ""
            return "<%s>%s</%s>" % (tag, inner, tag)
        if tag == "pre":
            return "\n\n%s\n\n" % self._code_block(node)
        if tag in BLOCK_TAGS:
            inner = " ".join(b for b in self._blocks(node) if b.strip())
            return (" " + inner + " ") if inner.strip() else ""
        return self._inline_children(node)

    def _anchor(self, node: Node) -> str:
        text = self._inline_children(node)
        href = (node.get("href") or "").strip()
        if not href or href.startswith("javascript") or href in ("#", "about:blank"):
            return text
        if node.has_class("video-box") or "/video/" in href:
            text = text.strip() or "视频"
            return "[%s](%s)" % (text, self._abs_url(href))
        href = self._abs_url(href)
        if not text.strip():
            return ""
        if IMG_TOKEN_RE.search(text):
            # 图片外面套了链接: 保留链接, 别把 {{img}} 丢掉
            return "[%s](%s)" % (text.strip(), href)
        if text.strip() == href:
            return "<%s>" % href
        return "[%s](%s)" % (text.strip(), href)

    def _abs_url(self, href: str) -> str:
        href = (href or "").strip()
        for _ in range(2):
            m = re.match(r"^https?://link\.zhihu\.com/\?target=([^&]+)", href)
            if not m:
                break
            href = urllib.parse.unquote(m.group(1))
        if href.startswith("//"):
            href = "https:" + href
        elif href.startswith("/"):
            href = "https://www.zhihu.com" + href
        elif re.match(r"^(?:[\w-]+\.)*zhihu\.com/", href, re.I):
            href = "https://" + href
        elif re.match(r"^[\w.-]+\.(?:com|cn|net|org|io)/", href, re.I):
            href = "https://" + href
        return href

    @staticmethod
    def _inline_code(text: str) -> str:
        text = text.replace("\r", " ").replace("\n", " ").replace("\u00a0", " ")
        text = text.strip()
        if not text:
            return ""
        longest = max([len(m.group(0)) for m in _BACKTICK_RE.finditer(text)] or [0])
        ticks = "`" * max(1, longest + 1)
        pad = " " if (text.startswith("`") or text.endswith("`")) else ""
        return "%s%s%s%s%s" % (ticks, pad, text, pad, ticks)

    @staticmethod
    def _escape_text(text: str) -> str:
        if not text:
            return text
        return text.replace("<", "&lt;").replace(">", "&gt;").replace("\u200b", "")

# 抓取知乎
_API_ARTICLE = (
    "https://www.zhihu.com/api/v4/articles/%s"
    "?include=content,title,created,updated,voteup_count,comment_count,author,image_url"
)
_API_ANSWER = (
    "https://www.zhihu.com/api/v4/answers/%s"
    "?include=content,question,author,created_time,updated_time,voteup_count,comment_count"
)

def sign_zse96(url: str, cookie: str) -> Dict[str, str]:
    """生成 x-zse-96 签名 (知乎网页 API 需要). 这是社区逆向出来的算法, 知乎随时可能改; 失败时返回 {}, 调用方会回退到解析页面."""
    d_c0 = ""
    for part in (cookie or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key.strip() == "d_c0":
            d_c0 = value.strip()
            break
    if not d_c0:
        return {}
    try:
        parsed = urllib.parse.urlsplit(url)
        api_path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        zse93 = "101_3_3.0"
        raw = "%s+%s+%s" % (zse93, api_path, d_c0)
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()
        signature = base64.b64encode(digest.encode("utf-8")).decode("ascii")
        return {"x-zse-93": zse93, "x-zse-96": "2.0_" + signature}
    except Exception:
        return {}

class ZhihuClient:
    def __init__(self, http: HttpClient, debug_dir: Optional[Path] = None) -> None:
        self.http = http
        self.debug_dir = debug_dir

    # 对外
    def fetch(self, ref: Ref) -> Document:
        if ref.kind == "auto":
            errors: List[str] = []
            for kind in ("article", "answer"):
                try:
                    return self.fetch(Ref(kind, ref.target_id, "", ref.raw))
                except ZhihuError as exc:
                    errors.append("[%s] %s" % (kind, str(exc).splitlines()[0]))
            raise ZhihuError("没法确定 %s 是文章还是回答, 两种都试过了: \n  %s"
                             % (ref.target_id, "\n  ".join(errors)))
        try:
            return self._fetch_via_page(ref)
        except ZhihuError as page_error:
            debug("页面方式失败：%s" % str(page_error).splitlines()[0])
            try:
                return self._fetch_via_api(ref)
            except ZhihuError as api_error:
                raise ZhihuError("%s\n  （备用 v4 API 也失败了: %s）"
                                 % (page_error, str(api_error).splitlines()[0])) from None

    # 方式一: 解析页面内嵌 JSON
    def _fetch_via_page(self, ref: Ref) -> Document:
        url = ref.page_url
        debug("GET %s" % url)
        _status, headers, body = self.http.get(url)
        page = decode_text(body, headers)
        debug("页面 %d 字节" % len(page))

        state = extract_initial_state(page)
        if state is not None:
            entity = find_entity(state, ref.kind, ref.target_id)
            if entity is not None:
                return self._document_from_entity(entity, ref, state)
            debug("内嵌 JSON 里没有目标实体, 改试 DOM")

        dom_doc = self._fetch_via_dom(ref, page)
        if dom_doc is not None:
            return dom_doc

        self._save_debug(ref, page)
        raise ZhihuError(
            "页面里既没有内嵌数据, 也找不到正文容器 (知乎可能改了页面结构, 或被风控拦截). %s\n"
            "  可以加 --debug 复现并检查保存下来的 HTML; 也请确认链接本身能正常打开."
            % self._detect_block(page)
        )

    @staticmethod
    def _detect_block(page: str) -> str:
        """从页面里嗅一下失败原因, 给出更具体的中文提示."""
        sample = page[:60000]
        lower = sample.lower()
        checks = (
            (("被删除", "已删除", "内容不存在", "没有知识存在的荒原"), "\n  看起来这篇内容已经被删除或不可见了。"),
            (("安全验证", "unhuman", "captcha", "验证码", "security verification", "验证请求"),
             "\n  看起来触发了知乎的安全验证：用浏览器打开该页面过一次验证，再重试。"),
            (("登录", "signin", "sign_in", "login"), "\n  看起来是登录墙：请确认 Cookie 有效（重新复制一次）。"),
            (("付费", "盐选", "会员", "purchase"), "\n  这可能是付费 / 盐选内容，登录态下也需要购买才能看到全文。"),
        )
        for needles, hint in checks:
            if any(needle in lower for needle in needles):
                return hint
        return ""

    # 方式二: v4 API
    def _fetch_via_api(self, ref: Ref) -> Document:
        url = _API_ARTICLE % ref.target_id if ref.kind == "article" else _API_ANSWER % ref.target_id
        headers = {"x-requested-with": "fetch", "Accept": "application/json, text/plain, */*"}
        headers.update(sign_zse96(url, self.http.cookie))
        debug("GET %s" % url)
        _status, resp_headers, body = self.http.get(url, extra_headers=headers)
        text = decode_text(body, resp_headers)
        try:
            data = json.loads(text)
        except ValueError:
            raise ZhihuError("v4 API 返回的不是 JSON (%s)" % text[:120].replace("\n", " ")) from None
        if not isinstance(data, dict):
            raise ZhihuError("v4 API 返回结构异常")
        if data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise ZhihuError("v4 API 报错: %s" % message)
        return self._document_from_entity(data, ref, {})

    # 方式三: 直接从 DOM 里抠
    def _fetch_via_dom(self, ref: Ref, page: str) -> Optional[Document]:
        root = parse_html(page)

        def is_rich(node: Node) -> bool:
            return "RichText" in node.classes or "Post-RichText" in node.classes

        candidates: List[Node] = []
        for node in root.find_all(lambda n: n.tag in ("div", "article", "section", "main")):
            if is_rich(node) or len(node.find_all_tags("p")) >= 3:
                candidates.append(node)
        if not candidates:
            return None
        # 优先带 RichText 标记的、正文最长的那个容器
        candidates.sort(key=lambda n: (1 if is_rich(n) else 0, len(n.get_text())), reverse=True)
        content_el = candidates[0]
        content_html = _inner_html(content_el)
        if len(content_html) < 80:
            return None
        if ref.kind == "answer" and sum(1 for node in candidates if is_rich(node)) > 1:
            warn("页面里有多段正文, 兜底解析保存的是最长的那段, 请核对是否为想要的回答.")

        title = self._dom_title(root)
        author = self._dom_author(root)
        doc = Document(
            kind=ref.kind,
            target_id=ref.target_id,
            title=title,
            author=author,
            url=ref.canonical_url,
            content_html=content_html,
            question_id=ref.question_id if ref.kind == "answer" else "",
            source_note="DOM",
        )
        if ref.kind == "answer":
            doc.question_title = title
        debug("通过 DOM 兜底拿到正文 %d 字节" % len(content_html))
        return doc

    @staticmethod
    def _dom_title(root: Node) -> str:
        for pred in (
            lambda n: n.tag == "h1" and (n.class_contains("Post-Title") or n.class_contains("QuestionHeader")),
            lambda n: n.tag == "h1",
            lambda n: n.tag == "meta" and n.get("property") == "og:title",
            lambda n: n.tag == "title",
        ):
            node = root.find(pred)
            if node is None:
                continue
            text = node.get("content") if node.tag == "meta" else node.get_text()
            text = squash_ws(text or "").strip()
            text = re.sub(r"\s*[-–—|]\s*知乎\s*$", "", text)
            if text:
                return text
        return ""

    @staticmethod
    def _dom_author(root: Node) -> str:
        node = root.find(
            lambda n: n.tag == "span" and (n.class_contains("AuthorInfo-name") or n.class_contains("UserLink"))
        )
        if node is not None:
            text = squash_ws(node.get_text()).strip()
            if text:
                return text
        meta = root.find(lambda n: n.tag == "meta" and n.get("name") == "author")
        if meta is not None:
            return squash_ws(meta.get("content")).strip()
        return ""

    # 实体 → Document
    def _document_from_entity(self, entity: dict, ref: Ref, state: dict) -> Document:
        doc = Document(kind=ref.kind, target_id=ref.target_id, url=ref.canonical_url)
        content = entity.get("content")
        if isinstance(content, dict):  # 少数接口返回结构化内容
            content = content.get("html") or ""
        if not isinstance(content, str):
            content = ""
        doc.content_html = content

        if ref.kind == "article":
            doc.title = squash_ws(str(entity.get("title") or "")).strip()
            doc.created = fmt_time(entity.get("created") or entity.get("created_time"))
            doc.updated = fmt_time(entity.get("updated") or entity.get("updated_time"))
        else:
            question_title, question_id = resolve_question(entity, state)
            doc.question_title = question_title
            doc.question_id = question_id or ref.question_id
            if doc.question_id:
                doc.url = "https://www.zhihu.com/question/%s/answer/%s" % (doc.question_id, ref.target_id)
            doc.created = fmt_time(entity.get("created_time") or entity.get("created"))
            doc.updated = fmt_time(entity.get("updated_time") or entity.get("updated"))

        doc.author, doc.author_url = resolve_author(entity, state)
        for attr in ("voteup_count", "comment_count"):
            value = entity.get(attr)
            if isinstance(value, int):
                value = "{:,}".format(value)
            elif value:
                value = str(value)
            else:
                value = ""
            if attr == "voteup_count":
                doc.voteup = value
            else:
                doc.comments = value

        if not doc.content_html.strip():
            raise ZhihuError(
                "拿到内容了但正文是空的 (可能是付费内容、盐选专栏等)."
            )

        if not doc.title and ref.kind == "article":
            doc.title = self._first_heading(doc.content_html)
        return doc

    @staticmethod
    def _first_heading(content_html: str) -> str:
        root = parse_html(content_html)
        node = root.find(lambda n: n.tag in ("h1", "h2", "h3"))
        return squash_ws(node.get_text()).strip() if node is not None else ""

    def _save_debug(self, ref: Ref, page: str) -> None:
        if not self.debug_dir:
            return
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            path = self.debug_dir / ("%s-%s.html" % (ref.kind, ref.target_id))
            path.write_text(page, encoding="utf-8")
            warn("已保存原始 HTML 到 %s" % path)
        except OSError as exc:
            warn("保存调试 HTML 失败: %s" % exc)


def _inner_html(node: Node) -> str:
    """把一个子树重新序列化成 HTML 字符串."""
    parts: List[str] = []

    def walk(cur: Node) -> None:
        for child in cur.children:
            if child.tag is None:
                parts.append(_escape_html(child.text))
                continue
            attrs = "".join(
                ' %s="%s"' % (key, _escape_html(value, quote=True))
                for key, value in child.attrs.items()
            )
            if child.tag in VOID_TAGS:
                parts.append("<%s%s>" % (child.tag, attrs))
                continue
            parts.append("<%s%s>" % (child.tag, attrs))
            walk(child)
            parts.append("</%s>" % child.tag)

    walk(node)
    return "".join(parts)


def _escape_html(text: str, quote: bool = False) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    if quote:
        text = text.replace('"', "&quot;")
    return text

# 保存 
EXT_BY_TYPE = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "image/avif": ".avif",
    "image/bmp": ".bmp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "image/heic": ".heic",
    "image/tiff": ".tiff",
}

_EXT_RE = re.compile(r"\.(jpg|jpeg|png|gif|webp|svg|avif|bmp|ico|tiff|heic)$", re.I)

def guess_image_ext(content_type: str, url: str) -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in EXT_BY_TYPE:
        return EXT_BY_TYPE[ctype]
    if ctype.startswith("image/"):
        sub = ctype[len("image/"):].strip()
        if sub and re.match(r"^[a-z0-9]{2,5}$", sub):
            return "." + ("jpg" if sub == "jpeg" else sub)
    path = urllib.parse.urlsplit(url).path
    m = _EXT_RE.search(path)
    if m:
        ext = m.group(1).lower()
        return ".jpg" if ext == "jpeg" else "." + ext
    return ".jpg"

class ImageStore:
    """下载正文图片, 返回「第几张图片 → 相对路径」的映射."""
    def __init__(
        self,
        http: HttpClient,
        dest_dir: Path,
        rel_dir: str,
        page_url: str = ZHIHU_HOME,
        force: bool = False,
        workers: int = 4,
    ) -> None:
        self.http = http
        self.dest_dir = dest_dir
        self.rel_dir = rel_dir.strip("/")
        self.page_url = page_url or ZHIHU_HOME
        self.force = force
        self.workers = max(1, workers)
        self.ok = 0
        self.failed = 0

    def fetch_all(self, images: List[ImageRef]) -> Dict[int, str]:
        if not images:
            return {}
        try:
            self.dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            warn("创建图片目录失败: %s" % exc)
            return {}

        first_index: Dict[str, int] = {}
        unique: List[Tuple[int, ImageRef]] = []
        for index, image in enumerate(images):
            if image.url in first_index:
                continue
            first_index[image.url] = index
            unique.append((index, image))

        results: Dict[int, str] = {}
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self._download_one, image, "img-%03d" % (index + 1)): index
                for index, image in unique
            }
            for future, index in futures.items():
                try:
                    ext = future.result()
                except ZhihuError as exc:
                    warn("图片 %d 下载失败: %s" % (index + 1, exc))
                    continue
                except Exception as exc:  # pragma: no cover - 兜底
                    warn("图片 %d 下载失败: %r" % (index + 1, exc))
                    continue
                if ext:
                    results[index] = "%s/img-%03d%s" % (self.rel_dir, index + 1, ext)

        for index, image in enumerate(images):
            if index in results:
                continue
            src_index = first_index.get(image.url)
            if src_index is not None and src_index in results:
                results[index] = results[src_index].replace(
                    "img-%03d" % (src_index + 1), "img-%03d" % (index + 1)
                )
        self.ok = len(results)
        self.failed = len(images) - len(results)
        return results

    def _download_one(self, image: ImageRef, base: str) -> str:
        if not self.force:
            for path in self.dest_dir.glob(base + ".*"):
                if path.is_file() and path.suffix != ".part":
                    return path.suffix
        if image.data_uri:
            data, ext = self._decode_data_uri(image.url)
        else:
            _status, headers, data = self.http.get(
                image.url,
                extra_headers={
                    "Referer": self.page_url,
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                },
            )
            if not data:
                raise ZhihuError("响应为空")
            head = data[:200].lstrip().lower()
            if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
                raise ZhihuError("返回的是 HTML (可能触发防盗链或需要 Cookie)")
            ext = guess_image_ext(self._content_type(headers), image.url)
        path = self.dest_dir / (base + ext)
        try:
            path.write_bytes(data)
        except OSError as exc:
            raise ZhihuError("写入失败: %s" % exc) from None
        return ext

    @staticmethod
    def _content_type(headers: Dict[str, str]) -> str:
        for key, value in headers.items():
            if key.lower() == "content-type":
                return value or ""
        return ""

    @staticmethod
    def _decode_data_uri(uri: str) -> Tuple[bytes, str]:
        m = re.match(r"data:([^;,]*)(;base64)?,(.*)$", uri, re.S)
        if not m:
            raise ZhihuError("无法解析 data URI")
        mime, is_b64, payload = m.group(1) or "image/png", m.group(2), m.group(3)
        ext = EXT_BY_TYPE.get(mime.lower(), ".png")
        if is_b64:
            try:
                data = base64.b64decode(payload)
            except ValueError:
                raise ZhihuError("data URI base64 解码失败") from None
        else:
            data = urllib.parse.unquote_to_bytes(payload)
        return data, ext

@dataclass
class SaveResult:
    path: Path
    images_ok: int = 0
    images_failed: int = 0
    chars: int = 0

class Saver:
    def __init__(
        self,
        outdir: Path,
        http: HttpClient,
        with_images: bool = True,
        assets_dirname: str = "assets",
        name_format_article: str = "{title}",
        name_format_answer: str = "{question} - {author}",
        force: bool = False,
        skip_existing: bool = False,
        workers: int = 4,
    ) -> None:
        self.outdir = Path(outdir)
        self.http = http
        self.with_images = with_images
        self.assets_dirname = assets_dirname.strip("/") or "assets"
        self.name_format_article = name_format_article
        self.name_format_answer = name_format_answer
        self.force = force
        self.skip_existing = skip_existing
        self.workers = workers

    # 主流程
    def save(self, doc: Document) -> SaveResult:
        stem = self._stem(doc)
        md_path = self._unique_path(stem, doc)
        if self.skip_existing and md_path.exists():
            raise Skipped("已存在, 跳过: %s" % md_path.name)

        renderer = MarkdownRenderer(base_url=doc.url)
        body = renderer.render(parse_html(doc.content_html))

        mapping: Dict[int, str] = {}
        images_ok = images_failed = 0
        if self.with_images and renderer.images:
            asset_name = asset_slug(md_path.stem)
            rel_dir = "%s/%s" % (self.assets_dirname, asset_name)
            store = ImageStore(
                http=self.http,
                dest_dir=self.outdir / self.assets_dirname / asset_name,
                rel_dir=rel_dir,
                page_url=doc.url,
                force=self.force,
                workers=self.workers,
            )
            mapping = store.fetch_all(renderer.images)
            images_ok, images_failed = store.ok, store.failed
        body = IMG_TOKEN_RE.sub(lambda m: self._image_target(m, mapping, renderer.images), body)

        text = "\n\n".join(
            part for part in (
                build_frontmatter(doc),
                build_header(doc),
                body.strip(),
            ) if part
        ) + "\n"

        try:
            self.outdir.mkdir(parents=True, exist_ok=True)
            with open(md_path, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        except OSError as exc:
            raise ZhihuError("写文件失败: %s" % exc) from None
        return SaveResult(path=md_path, images_ok=images_ok, images_failed=images_failed,
                          chars=len(re.sub(r"\s", "", body)))

    @staticmethod
    def _image_target(match: "re.Match[str]", mapping: Dict[int, str], images: List[ImageRef]) -> str:
        index = int(match.group(1))
        if index in mapping:
            return mapping[index]
        if 0 <= index < len(images):
            return images[index].url
        return ""

    def _stem(self, doc: Document) -> str:
        fmt = self.name_format_article if doc.kind == "article" else self.name_format_answer
        values = {
            "title": doc.title or "",
            "question": doc.question_title or doc.title or "",
            "author": doc.author or "",
            "id": doc.target_id,
            "type": "文章" if doc.kind == "article" else "回答",
        }
        try:
            name = fmt.format(**values)
        except (KeyError, IndexError, ValueError):
            warn("文件名模板 %r 不合法, 改用默认模板" % fmt)
            name = values["title"] or values["question"] or "知乎%s" % doc.target_id
        name = re.sub(r"\s*-\s*$", "", name.strip())
        name = re.sub(r"^\s*-\s*", "", name).strip()
        name = safe_filename(name, max_len=80)
        if not name.strip(" -_"):
            name = "知乎%s-%s" % ("文章" if doc.kind == "article" else "回答", doc.target_id)
        return name

    def _unique_path(self, stem: str, doc: Document) -> Path:
        candidate = self.outdir / (stem + ".md")
        if not candidate.exists() or self._belongs_to(candidate, doc):
            return candidate
        for number in range(2, 1000):
            candidate = self.outdir / ("%s (%d).md" % (stem, number))
            if not candidate.exists() or self._belongs_to(candidate, doc):
                return candidate
        return self.outdir / ("%s-%s.md" % (stem, doc.target_id))

    @staticmethod
    def _belongs_to(path: Path, doc: Document) -> bool:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                head = handle.read(4000)
        except OSError:
            return False
        m = re.search(r'^zhihu_id:\s*"?(\d+)"?\s*$', head, re.M)
        if m and m.group(1) == doc.target_id:
            return True
        return False

def build_frontmatter(doc: Document) -> str:
    lines = ["---", "title: %s" % yaml_str(doc.display_title)]
    if doc.author:
        lines.append("author: %s" % yaml_str(doc.author))
    if doc.author_url:
        lines.append("author_url: %s" % yaml_str(doc.author_url))
    lines.append("source: %s" % yaml_str(doc.url))
    lines.append("zhihu_id: %s" % yaml_str(doc.target_id))
    lines.append("zhihu_type: %s" % yaml_str(doc.kind))
    if doc.question_title and doc.question_title != doc.display_title:
        lines.append("question: %s" % yaml_str(doc.question_title))
    if doc.question_id:
        lines.append("question_id: %s" % yaml_str(doc.question_id))
    for key, value in (("created", doc.created), ("updated", doc.updated)):
        if value:
            lines.append("%s: %s" % (key, yaml_str(value.replace(" ", "T"))))
    for key, value in (("voteup", doc.voteup), ("comments", doc.comments)):
        if value:
            lines.append("%s: %s" % (key, yaml_str(value)))
    lines.append("saved_at: %s" % yaml_str(datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))
    lines.append("tags:")
    lines.append("  - zhihu")
    lines.append("  - %s" % ("文章" if doc.kind == "article" else "回答"))
    lines.append("---")
    return "\n".join(lines)

def build_header(doc: Document) -> str:
    rows: List[str] = []
    who = "作者" if doc.kind == "article" else "回答者"
    if doc.author:
        name = "[%s](%s)" % (doc.author, doc.author_url) if doc.author_url else doc.author
        rows.append("%s：%s" % (who, name))
    meta: List[str] = []
    if doc.voteup:
        meta.append("赞同 %s" % doc.voteup)
    if doc.comments:
        meta.append("评论 %s" % doc.comments)
    if doc.created:
        meta.append("发布 %s" % doc.created)
    if doc.updated and doc.updated != doc.created:
        meta.append("更新 %s" % doc.updated)
    if meta:
        rows.append(" · ".join(meta))
    rows.append("原文：<%s>" % doc.url)

    title = squash_ws(doc.display_title).replace("\n", " ").strip() or "知乎内容"
    quote = "\n".join("> " + row for row in rows)
    return "# %s\n\n%s" % (title, quote)

# 命令行
def read_cookie_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ZhihuError("读取 Cookie 文件失败: %s" % exc) from None
    return normalize_cookie(text)

def parse_cookie_text(text: str) -> str:
    """兼容三种格式: `k=v; k=v` 字符串、Netscape cookies.txt、浏览器导出的 JSON."""
    stripped = (text or "").strip()
    if stripped[:1] in ("[", "{"):
        cookie = _parse_cookie_json(stripped)
        if cookie:
            return cookie

    pairs: "Dict[str, str]" = {}
    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#"):
            continue
        if "\t" in line:
            fields = line.split("\t")
            if len(fields) >= 7 and "zhihu" in fields[0].lower():
                key, value = fields[5].strip(), fields[6].strip()
                if key:
                    pairs[key] = value
        elif "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            if key:
                pairs[key] = value.strip().rstrip(";")
    return "; ".join("%s=%s" % (k, v) for k, v in pairs.items())

def _parse_cookie_json(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    if isinstance(data, dict):
        data = data.get("cookies") or []
    if not isinstance(data, list):
        return ""
    pairs: "Dict[str, str]" = {}
    for item in data:
        if isinstance(item, dict):
            name = item.get("name") or item.get("key")
            value = item.get("value")
            if isinstance(name, str) and isinstance(value, str):
                pairs[name] = value
    return "; ".join("%s=%s" % (k, v) for k, v in pairs.items())

_CURL_RE = re.compile(r"\bcurl(?:\.exe)?\b", re.I)
_CURL_HEADER_RE = re.compile(r"""(?:-H|--header)\s+(?:"([^"]*)"|'([^']*)'|(\S+))""", re.I)
_CURL_COOKIE_FLAG_RE = re.compile(r"""(?:-b|--cookie)\s+(?:"([^"]*)"|'([^']*)'|(\S+))""", re.I)
_HEADER_COOKIE_RE = re.compile(r"^\s*cookie\s*:\s*(.+)$", re.I | re.M)
_ANY_COOKIE_RE = re.compile(r"""cookie\s*:\s*([^"'\r\n]+)""", re.I)
# 表格式粘贴：`z_c0<TAB>值` 或 `z_c0    值`
_COOKIE_PAIR_LINE_RE = re.compile(r"^\s*([A-Za-z_][\w.-]{0,40})(?:\t+|\s{2,})(\S.*?)\s*$")
_KNOWN_COOKIE_KEYS = ("z_c0", "d_c0", "_xsrf", "_zap", "__zse_ck", "BEC", "q_c1", "tst", "tst")

def _clean_cookie_value(value: str) -> str:
    text = (value or "").replace("\r", "").replace("\n", " ")
    text = text.replace("\\ ", " ")  # 去掉 cURL 的续行反斜杠
    text = re.sub(r"\s+", " ", text).strip().strip(";").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:].strip()
    return text


def _cookie_from_curl(text: str) -> str:
    """从浏览器「Copy as cURL」的内容里取 cookie 头."""
    if not _CURL_RE.search(text):
        return ""
    cleaned = text.replace('^"', '"').replace("^'", "'")  # cmd 风格转义
    for match in _CURL_HEADER_RE.finditer(cleaned):
        header = next((g for g in match.groups() if g), "")
        name, _, value = header.partition(":")
        if name.strip().lower() == "cookie" and value.strip():
            return value.strip()
    for match in _CURL_COOKIE_FLAG_RE.finditer(cleaned):
        value = next((g for g in match.groups() if g), "").strip()
        if "=" in value:
            return value
    return ""

def _cookie_from_any_header(text: str) -> str:
    """任意文本里出现 `cookie: ...` 就抽出来 (兼容只复制了 -H 片段的 cURL)."""
    cleaned = text.replace('^"', '"').replace("^'", "'")
    match = _ANY_COOKIE_RE.search(cleaned)
    if not match:
        return ""
    value = match.group(1).strip()
    return value if "=" in value else ""

def _cookie_from_header_text(text: str) -> str:
    """从整段请求头里取 cookie."""
    match = _HEADER_COOKIE_RE.search(text or "")
    return match.group(1).strip() if match else ""

def _cookie_from_pairs_table(text: str) -> str:
    """兼容从 DevTools「Cookies / 请求 Cookie」表格粘出来的两列文本."""
    pairs: Dict[str, str] = {}
    for line in (text or "").splitlines():
        match = _COOKIE_PAIR_LINE_RE.match(line)
        if not match:
            continue
        name, value = match.group(1).strip(), match.group(2).strip()
        if name.lower() in ("name", "值", "value", "名称"):
            continue
        pairs[name] = value
    if any(key in pairs for key in ("z_c0", "d_c0")):
        return "; ".join("%s=%s" % (k, v) for k, v in pairs.items())
    return ""

def _cookie_from_bare_token(text: str) -> str:
    """只复制了 z_c0 的值（形如 2|1:0|10:16...|4:z_c0|92:...）时自动补键名."""
    token = _clean_cookie_value(text)
    if not token or "=" in token or " " in token or len(token) < 40:
        return ""
    if "|" in token or token.count(":") >= 2:
        return "z_c0=" + token
    return ""

def cookie_pairs(cookie: str) -> Dict[str, str]:
    """把 `k=v; k=v` 拆成字典."""
    pairs: Dict[str, str] = {}
    for part in (cookie or "").split(";"):
        chunk = part.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        key = key.strip()
        if key:
            pairs[key] = value.strip()
    return pairs

def validate_cookie(cookie: str) -> List[str]:
    """检查 Cookie 是否像一份可用的知乎登录态, 返回问题列表."""
    if not (cookie or "").strip():
        return ["没有找到 Cookie"]
    pairs = cookie_pairs(cookie)
    problems: List[str] = []
    if "z_c0" not in pairs:
        problems.append(
            "缺少 z_c0 (知乎的登录凭证), 说明复制到的不是登录后的请求, 或者 Cookie 不完整/已过期"
        )
    elif len(pairs["z_c0"]) < 20:
        problems.append("z_c0 太短 (只有 %d 字符), 可能没复制全" % len(pairs["z_c0"]))
    if "d_c0" not in pairs:
        problems.append("缺少 d_c0 —— 建议从同一个请求里一起复制")
    if not any(key in pairs for key in _KNOWN_COOKIE_KEYS):
        problems.append("一个知乎 Cookie 字段都没认出来, 可能粘错了内容")
    return problems

def normalize_cookie(raw: str) -> str:
    """把各种来源的 Cookie 归一化成 `k=v; k=v`.
    支持: 裸 Cookie 串、`cookie: ...` 整行/整段请求头、只复制了 -H 片段的 cURL、
    完整「Copy as cURL」、DevTools Cookie 表格的两列粘贴、单独的 z_c0 值、
    Netscape cookies.txt、浏览器导出的 JSON.
    """
    text = (raw or "").strip()
    if not text:
        return ""

    for extractor in (
        _cookie_from_curl,
        _cookie_from_any_header,
        _cookie_from_header_text,
        _cookie_from_pairs_table,
        _cookie_from_bare_token,
    ):
        value = extractor(text)
        if value:
            return _clean_cookie_value(value)

    if text.lower().startswith("cookie:"):
        text = text[7:]
    if "\n" in text or "\t" in text:
        return parse_cookie_text(text)
    text = re.sub(r"\s+", " ", text).strip().strip(";").strip()
    return text if "=" in text else ""

def load_cookie(explicit: str, cookie_file: str) -> str:
    if explicit:
        return normalize_cookie(explicit)
    if cookie_file:
        return read_cookie_file(Path(cookie_file))
    env = os.environ.get("ZHIHU_COOKIE", "")
    if env.strip():
        return normalize_cookie(env)
    candidates = [
        Path("cookie.txt"),
        Path.home() / ".zhihu2md-cookie",
        Path.home() / ".config" / "zhihu2md" / "cookie.txt",
    ]
    for candidate in candidates:
        if candidate.is_file():
            cookie = read_cookie_file(candidate)
            if cookie:
                debug("使用 Cookie 文件：%s" % candidate)
                return cookie
    return ""

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zhihu2md",
        description="把知乎专栏文章 / 回答保存为 Obsidian 友好的 Markdown (图片本地化).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
例子:
  python zhihu2md.py p/2094715383932573257
  python zhihu2md.py question/1914329673120347774/answer/2024747872659285839
  python zhihu2md.py -f links.txt -o ./知乎收藏
  python zhihu2md.py p/123 --cookie "z_c0=xxx; d_c0=yyy"

Cookie 也可以放在 ./cookie.txt、~/.zhihu2md-cookie,
或设置环境变量 ZHIHU_COOKIE.
""",
    )
    parser.add_argument("refs", nargs="*", help="知乎编号或链接 (可一次给多个)")
    parser.add_argument("-f", "--file", help="文本文件, 每行一个编号/链接 (用 - 表示标准输入)")
    parser.add_argument("-o", "--outdir", default="zhihu-archive", help="输出目录 (默认 ./zhihu-archive)")
    parser.add_argument("--cookie", default="", help="知乎 Cookie 字符串")
    parser.add_argument("--cookie-file", default="", help="Cookie 文件 (字符串或 Netscape 格式)")
    parser.add_argument("--no-images", action="store_true", help="只保存文字, 不下载图片")
    parser.add_argument("--assets-dir", default="assets", help="图片目录名 (默认 assets)")
    parser.add_argument("--name-format", default="", help="文章文件名模板, 支持 {title}/{question}/{author}/{id}")
    parser.add_argument("--answer-name-format", default="", help="回答文件名模板, 默认 {question} - {author}")
    parser.add_argument("--force", action="store_true", help="重新下载已存在的图片")
    parser.add_argument("--skip-existing", action="store_true", help="已存在同名文章时跳过")
    parser.add_argument("--timeout", type=float, default=30.0, help="单次请求超时秒数 (默认 30)")
    parser.add_argument("--retries", type=int, default=3, help="失败重试次数 (默认 3)")
    parser.add_argument("--proxy", default="", help="HTTP/HTTPS 代理, 例如 http://127.0.0.1:7890")
    parser.add_argument("--workers", type=int, default=4, help="图片并发下载线程数 (默认 4)")
    parser.add_argument("--delay", type=float, default=1.0, help="多条之间间隔秒数 (默认 1, 避免触发风控)")
    parser.add_argument("--debug", action="store_true", help="输出调试信息, 并把抓取失败的原始 HTML 保存下来")
    parser.add_argument("-v", "--verbose", action="store_true", help="同 --debug 的日志级别")
    parser.add_argument("--version", action="version", version="zhihu2md %s" % __version__)
    return parser

_EMBED_REF_RE = re.compile(r"question/\d+/answer/\d+|/answer/\d+|/p/\d+|answer/\d+|p/\d+")

def _collect_refs(args: argparse.Namespace) -> Tuple[List[Ref], List[Tuple[str, str]]]:
    """收集要抓的编号，返回 (有效的 Ref 列表, 无法识别的行).
    除了「一行一个编号」, 也能直接喂一整段从收藏夹/网页里复制出来的文字:
    识别不出来时会把行内出现的链接都抠出来.
    """
    lines: List[str] = list(args.refs or [])
    if args.file:
        if args.file == "-":
            content = sys.stdin.read()
        else:
            path = Path(args.file)
            try:
                content = path.read_text(encoding="utf-8-sig", errors="replace")
            except OSError as exc:
                raise ZhihuError("读取列表文件失败: %s" % exc) from None
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            lines.append(line)

    refs: List[Ref] = []
    bad: List[Tuple[str, str]] = []
    seen = set()
    for line in lines:
        candidates: List[Ref] = []
        try:
            candidates.append(parse_ref(line.strip().strip(",")))
        except ZhihuError:
            for match in _EMBED_REF_RE.finditer(line):
                try:
                    candidates.append(parse_ref(match.group(0)))
                except ZhihuError:
                    continue
        if not candidates:
            bad.append((line[:80], "无法识别的编号/链接"))
            continue
        for ref in candidates:
            key = (ref.kind, ref.target_id, ref.question_id)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
    return refs, bad

def main(argv: Optional[Sequence[str]] = None) -> int:
    _setup_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    global _VERBOSE
    _VERBOSE = bool(args.verbose or args.debug)

    try:
        refs, bad = _collect_refs(args)
    except ZhihuError as exc:
        warn(str(exc))
        return 2
    for line, reason in bad:
        warn("跳过一行（%s）：%s" % (reason, line))
    if not refs:
        if not bad:
            parser.print_help()
        return 2

    try:
        cookie = load_cookie(args.cookie, args.cookie_file)
    except ZhihuError as exc:
        warn(str(exc))
        return 2
    if not cookie:
        warn(
            "没有找到 Cookie: 知乎对未登录请求经常直接返回 403.\n"
            "  建议 (任选其一): \n"
            "    a) DevTools → Network → 点一个 www.zhihu.com/api/... 请求 → 右键「Copy as cURL」\n"
            "       → 整段粘给 --cookie, 程序会自动识别;\n"
            "    b) Application → Cookies → https://www.zhihu.com → 复制 z_c0 和 d_c0, \n"
            "       拼成 \"z_c0=...; d_c0=...\";\n"
            "    c) 请求头里 cookie: 那一行整行复制.\n"
        )
    else:
        problems = validate_cookie(cookie)
        for problem in problems:
            warn("Cookie 检查：%s" % problem)
        if problems:
            warn("  当前拿到的 Cookie 共 %d 字符, 请按 README 提示重新取一次." % len(cookie))

    outdir = Path(args.outdir).expanduser()
    try:
        outdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        warn("无法创建输出目录 %s：%s" % (outdir, exc))
        return 2

    http = HttpClient(cookie=cookie, timeout=args.timeout, retries=args.retries, proxy=args.proxy)
    debug_dir = outdir / "_debug" if args.debug else None
    client = ZhihuClient(http, debug_dir=debug_dir)
    saver = Saver(
        outdir=outdir,
        http=http,
        with_images=not args.no_images,
        assets_dirname=args.assets_dir,
        name_format_article=args.name_format or "{title}",
        name_format_answer=args.answer_name_format or (args.name_format or "{question} - {author}"),
        force=args.force,
        skip_existing=args.skip_existing,
        workers=args.workers,
    )

    log("输出目录: %s" % outdir.resolve())
    log("共 %d 条待保存%s" % (len(refs), " (含图片下载) " if not args.no_images else " (不下载图片) "))
    ok = failed = skipped = 0
    failures: List[Tuple[str, str]] = []
    for index, ref in enumerate(refs, 1):
        label = ref.raw or ref.canonical_url
        log("")
        log("[%d/%d] %s" % (index, len(refs), label))
        if index > 1 and args.delay > 0:
            time.sleep(args.delay)
        try:
            doc = client.fetch(ref)
            result = saver.save(doc)
        except Skipped as exc:
            skipped += 1
            log("  跳过: %s" % exc)
            continue
        except ZhihuError as exc:
            failed += 1
            failures.append((label, str(exc)))
            warn("失败: %s" % str(exc).replace("\n", "\n       "))
            continue
        except KeyboardInterrupt:
            warn("已中断")
            return 130
        except Exception as exc:  # pragma: no cover - 兜底
            failed += 1
            failures.append((label, repr(exc)))
            warn("出现意外错误: %r" % exc)
            if _VERBOSE:
                import traceback

                traceback.print_exc()
            continue

        ok += 1
        extra = ""
        if not args.no_images and (result.images_ok or result.images_failed):
            extra = "，图片 %d 张" % result.images_ok
            if result.images_failed:
                extra += " (%d 张失败) " % result.images_failed
        log("  已保存：%s (约 %d 字%s) " % (result.path, result.chars, extra))

    log("")
    log("完成: 成功 %d 条, 跳过 %d 条, 失败 %d 条." % (ok, skipped, failed))
    if failures:
        log("失败清单:")
        for item, reason in failures:
            log("  - %s :: %s" % (item, reason.splitlines()[0]))
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())