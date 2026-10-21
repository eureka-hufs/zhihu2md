#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""修复已经导出的 Markdown 里"空格被写成 +"的公式.

1.1.0 版本解析知乎公式时用了 urllib.parse.unquote, 遇到知乎的
form-urlencoded 编码（空格 -> '+'）会把空格变成一串字面加号,
导出结果里 $$...$$ 或 $...$ 中出现 `+++++`、`=+`、`+\int` 之类的东西.

本工具就地修复这些公式 (代码块 / 行内代码里的内容不会被动):

    python tools/fix_formula_plus.py 文件或目录 [更多路径...] [--dry-run] [--no-backup]

判定规则与 zhihu2md.decode_tex_param 一致: 只有"证据"足够（连续加号、
加号紧贴结构符号）才判定为被编码过; 连续 2~3 个加号夹在操作数之间视为
真正的加号运算符, 其余视为空格.

由 deepseek-v41-flash 生成.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zhihu2md as core  # noqa: E402

MATH_BLOCK_RE = re.compile(r"\$\$(.+?)\$\$", re.S)
INLINE_MATH_RE = re.compile(r"(?<!\$)\$(?!\$)([^$\n]{1,300}?)\$(?!\$)")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]")
# 公式里允许出现中文, 只要它在 \text{...} 这类命令里
_TEX_WORD_RE = re.compile(
    r"\\(?:text|textrm|textbf|textit|textsf|texttt|mathrm|operatorname|mbox|hbox)\{[^{}]*\}"
)
FENCE_RE = re.compile(r"^[ \t]*```[^\n]*\n.*?\n[ \t]*```[ \t]*$", re.S | re.M)
ALIGN_BEGIN_RE = re.compile(r"\\begin\{(?:align\*?|eqnarray\*?|gather\*?|split)\}")
ALIGN_END_RE = re.compile(r"\\end\{(?:align\*?|eqnarray\*?|gather\*?|split)\}")

def looks_like_math(text: str) -> bool:
    """行内 $...$ 里必须是公式, 别把两个公式之间的中文当成公式.
    中文允许出现在 `\\text{...}` 这类命令里.
    1.1.2 update: 不再要求公式里必须有 '+'. 因为"只残留结尾 \\"的公式
    (例如 $x=1\\\\$) 也需要修, 旧版本会因为不含 '+' 直接跳过.
    """
    text = text or ""
    probe = _TEX_WORD_RE.sub("", text)
    if _CJK_RE.search(probe):
        return False
    return "\\" in text or "^" in text or "_" in text or "=" in text

_TRAILING_BREAK_RE = re.compile(r"\\+\s*$")

def _tidy(body: str, display: bool, fix_plus: bool = True) -> str:
    """整理一段公式. fix_plus=False 时只处理结尾换行/空白, 不动加号.
    1.1.2 update: 必须区分两种修复, 否则"只删结尾 \\"时会顺手把
    `a+b` 的加号当成空格吃掉.
    """
    if fix_plus:
        body = core._expand_plus_runs(body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = body.replace("\\left [", "\\left[").replace("\\right ]", "\\right]")
    body = core.strip_trailing_breaks(body)   # 1.1.2: 两种模式都去掉结尾 \ / \\
    if display:
        body = ALIGN_BEGIN_RE.sub(lambda _m: "\\begin{aligned}", body)
        body = ALIGN_END_RE.sub(lambda _m: "\\end{aligned}", body)
        return "\n" + body + "\n"
    return body

def _needs_repair(body: str) -> tuple[bool, bool]:
    """返回 (要不要处理, 要不要连加号一起修)."""
    text = (body or "").strip()
    if not text:
        return False, False
    needs_tail = bool(_TRAILING_BREAK_RE.search(text))
    needs_plus = core.plus_repair_suspicion(text)
    return (needs_tail or needs_plus), needs_plus

def repair_block(body: str) -> str:
    """修复一个 $$ 块 (含换行缩进): 不值得修就原样返回."""
    needs, fix_plus = _needs_repair(body)
    if not needs:
        return body
    return _tidy(body, display=True, fix_plus=fix_plus)

def repair_inline(body: str) -> str:
    needs, fix_plus = _needs_repair(body)
    if not needs:
        return body
    return _tidy(body, display=False, fix_plus=fix_plus)

def _repair_inline_in_text(text: str) -> tuple[str, int]:
    """处理整段普通文本里的行内公式, 跳过反引号代码."""
    count = 0

    def fix_line(line: str) -> str:
        nonlocal count
        pieces = re.split(r"((?:`+)[^`]*?(?:`+))", line)
        out = []
        for index, piece in enumerate(pieces):
            if index % 2 == 1:  # 行内代码, 原样保留
                out.append(piece)
                continue

            def repl(match: "re.Match[str]") -> str:
                nonlocal count
                body = match.group(1)
                if not looks_like_math(body):
                    return match.group(0)
                fixed = repair_inline(body)
                has_linebreak = "\\\\" in fixed
                if fixed == body and not has_linebreak:
                    return match.group(0)
                count += 1
                if has_linebreak:
                    # 1.1.2: 行内 $...$ 里不能有换行符, 改成整段公式
                    return "$$\n" + core.strip_trailing_breaks(fixed).strip() + "\n$$"
                return "$" + fixed + "$"

            out.append(INLINE_MATH_RE.sub(repl, piece))
        return "".join(out)

    return "\n".join(fix_line(line) for line in text.split("\n")), count

def repair_text(text: str) -> tuple[str, int]:
    fences: list[str] = []

    def hide(match: "re.Match[str]") -> str:
        fences.append(match.group(0))
        return "\x00FENCE%d\x00" % (len(fences) - 1)

    protected = FENCE_RE.sub(hide, text)

    blocks = 0

    def repl_block(match: "re.Match[str]") -> str:
        nonlocal blocks
        body = match.group(1)
        fixed = repair_block(body)
        if fixed != body:
            blocks += 1
            return "$$" + fixed + "$$"
        return match.group(0)

    protected = MATH_BLOCK_RE.sub(repl_block, protected)
    protected, inline_count = _repair_inline_in_text(protected)

    def restore(match: "re.Match[str]") -> str:
        return fences[int(match.group(1))]

    protected = re.sub(r"\x00FENCE(\d+)\x00", restore, protected)
    return protected, blocks + inline_count

def iter_md_files(paths: list[Path]):
    for path in paths:
        if path.is_dir():
            yield from sorted(path.rglob("*.md"))
        elif path.is_file() and path.suffix.lower() == ".md":
            yield path
        else:
            print("跳过 (不是 .md): %s" % path)

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="修复导出 Markdown 里被写成 + 的公式空格")
    parser.add_argument("paths", nargs="+", help="要处理的 .md 文件或目录")
    parser.add_argument("--dry-run", action="store_true", help="只报告, 不写文件")
    parser.add_argument("--no-backup", action="store_true", help="不生成 .bak 备份")
    args = parser.parse_args(argv)

    targets = list(iter_md_files([Path(p) for p in args.paths]))
    if not targets:
        print("没有找到 .md 文件")
        return 1

    total_files = changed_files = total_formulas = 0
    for path in targets:
        total_files += 1
        try:
            original = path.read_text(encoding="utf-8")
        except OSError as exc:
            print("读取失败 %s：%s" % (path, exc))
            continue
        fixed, count = repair_text(original)
        if not count:
            continue
        changed_files += 1
        total_formulas += count
        if args.dry_run:
            print("[dry-run] %s  ->  %d 处公式" % (path, count))
            continue
        if not args.no_backup:
            backup = path.with_suffix(path.suffix + ".bak")
            backup.write_text(original, encoding="utf-8")
        path.write_text(fixed, encoding="utf-8")
        print("已修复 %s  ->  %d 处公式%s" % (
            path, count, "" if args.no_backup else "（备份 %s）" % backup.name))

    print("\n扫描 %d 个文件, 修复 %d 个文件、共 %d 处公式%s" % (
        total_files, changed_files, total_formulas,
        "(dry-run, 未写盘)" if args.dry_run else ""))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())