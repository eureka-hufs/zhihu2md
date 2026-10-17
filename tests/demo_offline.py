#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线演示: 用 tests/fixtures 里的页面快照跑一遍完整流程, 输出到 samples/.
不联网, 用来检查「解析 → 转换 → 下载图片 → 写 .md」的结果长什么样.
运行:
    python tests/demo_offline.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import zhihu2md as z  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"
SAMPLES = ROOT / "samples"

ARTICLE_ID = "2094715383932573257"
ANSWER_ID = "2024747872659285839"
QUESTION_ID = "1914329673120347774"

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 512
GIF = b"GIF89a" + b"\x00" * 512

def response(path: Path):
    return 200, {"Content-Type": "text/html; charset=utf-8"}, path.read_bytes()

class FakeHttp(z.HttpClient):
    """只在本机返回固定内容, 模拟知乎服务器."""
    routes = {
        "/p/" + ARTICLE_ID: response(FIXTURES / "article_page.html"),
        "answer/" + ANSWER_ID: response(FIXTURES / "answer_page.html"),
    }

    def get(self, url, extra_headers=None, allow_status=()):
        for fragment, value in self.routes.items():
            if fragment in url:
                return value
        if "zhimg.com" in url:
            is_gif = url.endswith(".gif")
            return 200, {"Content-Type": "image/gif" if is_gif else "image/png"}, GIF if is_gif else PNG
        raise z.ZhihuError("演示里没有配置这个 URL: %s" % url)

def main() -> int:
    if SAMPLES.exists():
        shutil.rmtree(SAMPLES)
    http = FakeHttp()
    client = z.ZhihuClient(http)
    saver = z.Saver(SAMPLES, http)
    for raw in ("p/" + ARTICLE_ID, "question/%s/answer/%s" % (QUESTION_ID, ANSWER_ID)):
        doc = client.fetch(z.parse_ref(raw))
        result = saver.save(doc)
        print("已生成: %s (图片 %d 张) " % (result.path.relative_to(ROOT), result.images_ok))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
