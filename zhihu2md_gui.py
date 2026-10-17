#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
zhihu2md_gui.py 知乎内容 Markdown 备份的 Windows 图形界面
界面部分完成: 收集参数、开线程调用 zhihu2md 里的逻辑、日志显示.
"""

from __future__ import annotations
import io
import json
import os
import queue
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Callable, List, Optional
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import zhihu2md as core

APP_TITLE = "知乎内容 Markdown 备份"
APP_SUBTITLE = "将知乎文章 / 回答保存为本地 Markdown, 可直接导入 Obsidian"
SETTINGS_NAME = "gui-settings.json"
COOKIE_NAME = "cookie.txt"
ERROR_LOG = "gui-error.log"
NAME_MODES = {
    "标题优先（推荐）": ("{title}", "{question} - {author}"),
    "作者优先": ("{author} - {title}", "{author} - {question}"),
}

def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent

def resource_path(name: str) -> Path:
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidate = Path(base) / name
        if candidate.exists():
            return candidate
    return app_dir() / name

@dataclass
class JobParams:
    text: str = ""
    outdir: Path = field(default_factory=lambda: app_dir() / "zhihu-archive")
    cookie: str = ""
    with_images: bool = True
    skip_existing: bool = False
    proxy: str = ""
    name_mode: str = "标题优先（推荐）"
    workers: int = 4
    delay: float = 1.0


class Emitter:
    def __init__(self, put: Callable[[tuple], None]) -> None:
        self._put = put

    def log(self, text: str) -> None:
        self._put(("log", text))

    def progress(self, index: int, total: int) -> None:
        self._put(("progress", index, total))

    def done(self, summary: dict) -> None:
        self._put(("done", summary))


def collect_refs(text: str):
    feed = "\n".join(line for line in (text or "").splitlines() if line.strip())
    return core._collect_refs(SimpleNamespace(refs=[feed] if feed else [], file=None))


def run_job(params: JobParams, emit: Emitter, should_stop: Callable[[], bool]) -> dict:
    refs, bad = collect_refs(params.text)
    for line, reason in bad:
        emit.log("跳过一行（%s）：%s\n" % (reason, line))
    if not refs:
        emit.log("没有识别到任何知乎编号或链接.\n")
        return {"total": 0, "ok": 0, "failed": 0, "skipped": 0, "cancelled": False}

    outdir = Path(params.outdir)
    emit.log("输出目录: %s\n" % outdir)
    emit.log("共 %d 条待保存%s\n" % (len(refs), "(含图片下载)" if params.with_images else "(不下载图片)"))

    article_fmt, answer_fmt = NAME_MODES.get(params.name_mode, NAME_MODES["标题优先（推荐）"])
    http = core.HttpClient(cookie=params.cookie, timeout=30, retries=3, proxy=params.proxy)
    client = core.ZhihuClient(http)
    saver = core.Saver(
        outdir=outdir,
        http=http,
        with_images=params.with_images,
        name_format_article=article_fmt,
        name_format_answer=answer_fmt,
        force=False,
        skip_existing=params.skip_existing,
        workers=max(1, int(params.workers)),
    )

    ok = failed = skipped = 0
    cancelled = False
    for index, ref in enumerate(refs, 1):
        if should_stop():
            cancelled = True
            emit.log("\n已停止.\n")
            break
        emit.progress(index, len(refs))
        emit.log("\n[%d/%d] %s\n" % (index, len(refs), ref.raw or ref.canonical_url))
        if index > 1 and params.delay > 0:
            time.sleep(params.delay)
        try:
            doc = client.fetch(ref)
            result = saver.save(doc)
        except core.Skipped as exc:
            skipped += 1
            emit.log("  跳过: %s\n" % exc)
            continue
        except core.ZhihuError as exc:
            failed += 1
            emit.log("  失败: %s\n" % str(exc).replace("\n", "\n         "))
            continue
        except Exception as exc:  # pragma: no cover - 兜底
            failed += 1
            emit.log("  出现意外错误: %r\n" % exc)
            continue
        ok += 1
        extra = ""
        if params.with_images and (result.images_ok or result.images_failed):
            extra = "，图片 %d 张" % result.images_ok
            if result.images_failed:
                extra += "(%d 张失败)" % result.images_failed
        emit.log("  已保存: %s (约 %d 字%s)\n" % (result.path.name, result.chars, extra))

    summary = {"total": len(refs), "ok": ok, "failed": failed, "skipped": skipped,
               "cancelled": cancelled, "outdir": str(outdir)}
    return summary

class StreamToUI(io.TextIOBase):
    def __init__(self, emit: Emitter) -> None:
        super().__init__()
        self._emit = emit

    def writable(self) -> bool:
        return True

    def write(self, text: str) -> int:  # type: ignore[override]
        if text:
            self._emit.log(text)
        return len(text)

    def flush(self) -> None:
        return

class App:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.app_dir = app_dir()
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self.settings = self._load_settings()
        self._last_outdir = ""

        root.title("%s %s" % (APP_TITLE, core.__version__))
        root.geometry(self.settings.get("geometry") or "940x740")
        root.minsize(820, 620)
        self._log_handle = None
        self._log_path = self.app_dir / "gui-log.txt"
        self._open_log_file()
        self._set_icon()
        self._build_styles()
        self._build_widgets()
        self._apply_settings()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        # 兜底：任何 Tk 回调里未捕获的异常都弹窗 + 写日志，避免"点了没反应"
        self.root.report_callback_exception = self._report_callback_exception
        self.root.after(80, self._drain_queue)

    def _report_callback_exception(self, exc_type, exc_value, tb) -> None:
        detail = "".join(traceback.format_exception(exc_type, exc_value, tb))
        try:
            self._append_log("\n[界面内部错误]\n" + detail + "\n")
        except Exception:
            pass
        try:
            write_crash_log(detail)
        except Exception:
            pass
        try:
            messagebox.showerror(
                APP_TITLE,
                "界面处理出错：%s\n\n详细信息已写入 gui-error.log 和 gui-log.txt" % exc_value,
            )
        except Exception:
            pass

    def _open_log_file(self) -> None:
        """把界面日志同时写进 gui-log.txt, 方便出问题时直接发文件."""
        try:
            self._log_handle = open(self._log_path, "w", encoding="utf-8", buffering=1)
            self._log_handle.write("zhihu2md %s  %s  app_dir=%s\n"
                                   % (core.__version__, time.strftime("%Y-%m-%d %H:%M:%S"), self.app_dir))
        except OSError:
            self._log_handle = None

    def _set_icon(self) -> None:
        for name in ("zhihu2md.ico",):
            path = resource_path(name)
            if path.exists():
                try:
                    self.root.iconbitmap(default=str(path))
                    return
                except tk.TclError:
                    pass

    @staticmethod
    def _build_styles() -> None:
        style = ttk.Style()
        for theme in ("vista", "winnative", "clam"):
            if theme in style.theme_names():
                style.theme_use(theme)
                break
        base = ("Microsoft YaHei UI", 10)
        style.configure(".", font=base)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Hint.TLabel", foreground="#666666", font=("Microsoft YaHei UI", 9))
        style.configure("Accent.TButton", font=("Microsoft YaHei UI", 11, "bold"))
        style.configure("Status.TLabel", foreground="#1a6fb5", font=("Microsoft YaHei UI", 9, "bold"))
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=14)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        ttk.Label(outer, text=APP_TITLE, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(outer, text=APP_SUBTITLE, style="Hint.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 10))

        box_input = ttk.LabelFrame(outer, text=" 要保存的内容 ", padding=10)
        box_input.grid(row=2, column=0, sticky="nsew")
        box_input.columnconfigure(0, weight=1)
        ttk.Label(
            box_input,
            text="一行一个 /p 或 /answer 链接",
            style="Hint.TLabel",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.txt_input = scrolledtext.ScrolledText(
            box_input, height=6, wrap="word", font=("Consolas", 10), undo=True
        )
        self.txt_input.grid(row=1, column=0, sticky="nsew")
        box_input.rowconfigure(1, weight=1)

        # ② 设置
        box_opt = ttk.LabelFrame(outer, text=" 设置 ", padding=10)
        box_opt.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        box_opt.columnconfigure(1, weight=1)

        ttk.Label(box_opt, text="输出目录: ").grid(row=0, column=0, sticky="w")
        self.var_outdir = tk.StringVar()
        ttk.Entry(box_opt, textvariable=self.var_outdir).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(box_opt, text="浏览…", command=self._choose_outdir, width=8).grid(row=0, column=2)
        ttk.Button(box_opt, text="打开", command=self._open_outdir, width=6).grid(row=0, column=3, padx=(4, 0))

        ttk.Label(box_opt, text="Cookie: ").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.var_cookie = tk.StringVar()
        ttk.Entry(box_opt, textvariable=self.var_cookie, show="•").grid(
            row=1, column=1, sticky="ew", padx=6, pady=(8, 0)
        )
        ttk.Button(box_opt, text="读取文件", command=self._load_cookie_file, width=10).grid(
            row=1, column=2, sticky="ew", pady=(8, 0)
        )
        ttk.Button(box_opt, text="粘贴识别", command=self._paste_cookie, width=10).grid(
            row=1, column=3, sticky="ew", pady=(8, 0)
        )

        ttk.Label(box_opt, text="代理: ").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.var_proxy = tk.StringVar()
        ttk.Entry(box_opt, textvariable=self.var_proxy).grid(row=2, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(box_opt, text="保存 Cookie", command=self._save_cookie_file, width=14).grid(
            row=2, column=2, columnspan=2, sticky="ew", pady=(8, 0)
        )

        ttk.Label(
            box_opt,
            text="Cookie 支持直接粘 cURL、请求头 cookie 行, 点「粘贴识别」自动提取并检查",
            style="Hint.TLabel",
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(6, 0))

        ttk.Label(box_opt, text="命名方式: ").grid(row=3, column=0, sticky="w", pady=(8, 0))
        self.var_name_mode = tk.StringVar(value=list(NAME_MODES)[0])
        ttk.Combobox(
            box_opt, textvariable=self.var_name_mode, values=list(NAME_MODES), state="readonly", width=18
        ).grid(row=3, column=1, sticky="w", padx=6, pady=(8, 0))

        self.var_images = tk.BooleanVar(value=True)
        self.var_skip = tk.BooleanVar(value=False)
        ttk.Checkbutton(box_opt, text="下载图片到本地 (推荐)", variable=self.var_images).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(8, 0)
        )
        ttk.Checkbutton(box_opt, text="跳过已保存的内容", variable=self.var_skip).grid(
            row=4, column=2, columnspan=2, sticky="w", pady=(8, 0)
        )

        # ③ 操作
        box_run = ttk.Frame(outer)
        box_run.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        box_run.columnconfigure(2, weight=1)
        self.btn_start = ttk.Button(box_run, text="开始保存", style="Accent.TButton",
                                    command=self._on_start, width=14)
        self.btn_start.grid(row=0, column=0)
        self.btn_stop = ttk.Button(box_run, text="停止", command=self._on_stop, width=8, state="disabled")
        self.btn_stop.grid(row=0, column=1, padx=(8, 12))
        self.progress = ttk.Progressbar(box_run, mode="determinate", maximum=1)
        self.progress.grid(row=0, column=2, sticky="ew")
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(box_run, textvariable=self.var_status, style="Status.TLabel").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0)
        )

        # ④ 日志
        box_log = ttk.LabelFrame(outer, text=" 运行日志 ", padding=8)
        box_log.grid(row=5, column=0, sticky="nsew", pady=(10, 0))
        box_log.columnconfigure(0, weight=1)
        box_log.rowconfigure(0, weight=1)
        self.txt_log = scrolledtext.ScrolledText(
            box_log, height=12, wrap="word", font=("Consolas", 9), state="disabled"
        )
        self.txt_log.grid(row=0, column=0, sticky="nsew")

        outer.rowconfigure(5, weight=1)

        self._log_line("提示: 第一次使用需要知乎 Cookie. 最省事最不绕弯子路径: DevTools → Network →\n")
        self._log_line("      选择一个 www.zhihu.com/api 请求 → 右键「Copy as cURL」→ 回来点「粘贴识别」.\n")

    @property
    def _settings_path(self) -> Path:
        return self.app_dir / SETTINGS_NAME

    def _load_settings(self) -> dict:
        try:
            return json.loads(self._settings_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _apply_settings(self) -> None:
        outdir = self.settings.get("outdir") or str(self.app_dir / "zhihu-archive")
        self.var_outdir.set(outdir)
        self.var_images.set(bool(self.settings.get("with_images", True)))
        self.var_skip.set(bool(self.settings.get("skip_existing", False)))
        self.var_proxy.set(self.settings.get("proxy", ""))
        self.var_name_mode.set(self.settings.get("name_mode", list(NAME_MODES)[0]))
        self._load_cookie_file(silent=True)
        self.txt_input.focus_set()

    def _save_settings(self) -> None:
        data = {
            "outdir": self.var_outdir.get().strip(),
            "with_images": bool(self.var_images.get()),
            "skip_existing": bool(self.var_skip.get()),
            "proxy": self.var_proxy.get().strip(),
            "name_mode": self.var_name_mode.get(),
            "geometry": self.root.geometry(),
        }
        try:
            self._settings_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            pass

    # 日志
    def _append_log(self, text: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")
        if self._log_handle:
            try:
                self._log_handle.write(text)
            except (OSError, ValueError):
                pass

    def _log_line(self, text: str) -> None:
        self._append_log(text)

    def _drain_queue(self) -> None:
        try:
            while True:
                message = self.queue.get_nowait()
                kind = message[0]
                if kind == "log":
                    self._append_log(message[1])
                elif kind == "progress":
                    index, total = message[1], message[2]
                    self.progress.configure(maximum=max(1, total), value=index)
                    self.var_status.set("正在保存 %d/%d …" % (index, total))
                elif kind == "done":
                    self._on_finished(message[1])
        except queue.Empty:
            pass
        self.root.after(80, self._drain_queue)

    # 按钮
    def _choose_outdir(self) -> None:
        initial = self.var_outdir.get().strip() or str(self.app_dir)
        chosen = filedialog.askdirectory(title="选择输出目录 (建议放在 Obsidian 库里)", initialdir=initial)
        if chosen:
            self.var_outdir.set(chosen)

    def _open_outdir(self) -> None:
        target = Path(self.var_outdir.get().strip() or (self.app_dir / "zhihu-archive"))
        try:
            target.mkdir(parents=True, exist_ok=True)
            os.startfile(str(target))  # type: ignore[attr-defined]
        except Exception as exc:
            messagebox.showwarning(APP_TITLE, "打不开目录: %s" % exc)

    def _cookie_path(self) -> Path:
        return self.app_dir / COOKIE_NAME

    def _load_cookie_file(self, silent: bool = False) -> None:
        path = self._cookie_path()
        if not path.exists():
            if not silent:
                messagebox.showinfo(APP_TITLE, "还没有 cookie.txt. \n\n可以把 Cookie 粘贴到上面的输入框, 然后点「保存 Cookie」.")
            return
        try:
            cookie = core.normalize_cookie(path.read_text(encoding="utf-8-sig", errors="replace"))
        except OSError as exc:
            if not silent:
                messagebox.showwarning(APP_TITLE, "读不了 cookie.txt: %s" % exc)
            return
        if cookie:
            self.var_cookie.set(cookie)
            if not silent:
                self._log_line("已从 cookie.txt 载入 Cookie (%d 字符).\n" % len(cookie))
        elif not silent:
            messagebox.showinfo(APP_TITLE, "cookie.txt 里没有有效内容.")

    def _paste_cookie(self) -> None:
        """从剪贴板识别 Cookie: 支持 cURL 命令、请求头 cookie 行、裸 Cookie 串。"""
        try:
            text = self.root.clipboard_get()
        except tk.TclError:
            messagebox.showwarning(APP_TITLE, "剪贴板是空的.")
            return
        cookie = core.normalize_cookie(text)
        if not cookie:
            messagebox.showwarning(
                APP_TITLE,
                "剪贴板里没识别出 Cookie.\n\n"
                "可以复制这几种之一: \n"
                "  1. DevTools 里请求右键 → Copy as cURL (cmd)\n"
                "  2. 请求头里的 cookie 那一行\n",
            )
            return
        self.var_cookie.set(cookie)
        problems = core.validate_cookie(cookie)
        self._log_line("已从剪贴板识别 Cookie (%d 字符，%d 个字段).\n"
                       % (len(cookie), len(core.cookie_pairs(cookie))))
        if problems:
            for problem in problems:
                self._log_line("  Cookie 检查: %s\n" % problem)
            messagebox.showwarning(
                APP_TITLE,
                "Cookie 识别到了, 但检查没通过: \n\n" + "\n".join("· " + p for p in problems)
                + "\n\n最可能的原因: 复制错了请求 (比如 blob: 请求), 或者只复制了一部分.\n"
                  "请重新复制正确内容.",
            )
        else:
            self._log_line("  Cookie 检查通过. 点「保存 Cookie」可留到下次.\n")

    def _save_cookie_file(self) -> None:
        cookie = core.normalize_cookie(self.var_cookie.get())
        if not cookie:
            messagebox.showwarning(APP_TITLE, "Cookie 是空的, 先粘贴再保存.")
            return
        try:
            self._cookie_path().write_text(cookie + "\n", encoding="utf-8")
        except OSError as exc:
            messagebox.showwarning(APP_TITLE, "保存失败: %s" % exc)
            return
        self._log_line("Cookie 已保存到 %s\n" % self._cookie_path())

    def _on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        text = self.txt_input.get("1.0", "end").strip()
        if not text:
            messagebox.showwarning(APP_TITLE, "先粘贴知乎编号或链接. \n\n例如：p/12345678910111213145")
            self.txt_input.focus_set()
            return
        outdir = Path(self.var_outdir.get().strip() or (self.app_dir / "zhihu-archive"))
        cookie = core.normalize_cookie(self.var_cookie.get()) or core.load_cookie("", "")
        problems = core.validate_cookie(cookie)
        if not cookie:
            problems = ["没有 Cookie"]
        if problems:
            for problem in problems:
                self._log_line("Cookie 检查: %s\n" % problem)
            if not messagebox.askyesno(
                APP_TITLE,
                "Cookie 检查没通过: \n\n" + "\n".join("· " + p for p in problems)
                + "\n\n这种情况下知乎大概率返回 403.\n"
                  "最省事的做法: DevTools → Network → 点一个 www.zhihu.com/api/… 请求\n"
                  "→ 右键「Copy as cURL」→ 点「粘贴识别」/「保存 Cookie」.\n\n"
                  "仍然继续尝试吗？",
            ):
                return
        else:
            self._log_line("Cookie 检查通过 (共 %d 字符).\n" % len(cookie))
            if not self.var_cookie.get().strip():
                self.var_cookie.set(cookie)
        fields = sorted(core.cookie_pairs(cookie))
        self._log_line("Cookie 字段: %s (值不记录).\n" % (", ".join(fields) or "无"))

        params = JobParams(
            text=text,
            outdir=outdir,
            cookie=cookie,
            with_images=bool(self.var_images.get()),
            skip_existing=bool(self.var_skip.get()),
            proxy=self.var_proxy.get().strip(),
            name_mode=self.var_name_mode.get(),
        )
        self._save_settings()
        self._last_outdir = str(outdir)
        self.stop_event.clear()
        self.progress.configure(value=0)
        self.var_status.set("正在连接知乎 …")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self._append_log("\n" + "=" * 60 + "\n开始, 输出到 %s\n" % outdir)
        self.worker = threading.Thread(target=self._worker, args=(params,), daemon=True)
        self.worker.start()

    def _worker(self, params: JobParams) -> None:
        emit = Emitter(self.queue.put)
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = StreamToUI(emit)  # type: ignore[assignment]
        try:
            summary = run_job(params, emit, self.stop_event.is_set)
        except Exception:
            emit.log("\n程序内部错误: \n%s\n" % traceback.format_exc())
            summary = {"total": 0, "ok": 0, "failed": 1, "skipped": 0,
                       "cancelled": False, "outdir": str(params.outdir), "error": True}
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        emit.done(summary)

    def _on_stop(self) -> None:
        self.stop_event.set()
        self.btn_stop.configure(state="disabled")
        self.var_status.set("正在停止 (等当前这篇处理完)…")

    def _on_finished(self, summary: dict) -> None:
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        if summary.get("cancelled"):
            self.var_status.set("已停止: 成功 %d, 跳过 %d, 失败 %d"
                                % (summary["ok"], summary.get("skipped", 0), summary["failed"]))
            return
        self.progress.configure(value=self.progress["maximum"])
        text = "完成: 成功 %d 条, 跳过 %d 条, 失败 %d 条. " % (
            summary["ok"], summary.get("skipped", 0), summary["failed"])
        self.var_status.set(text)
        self._append_log("\n" + text + "\n")
        if summary.get("total") == 0:
            messagebox.showwarning(APP_TITLE, "没有识别到任何知乎编号或链接.")
            return
        if summary.get("error"):
            messagebox.showerror(APP_TITLE, "程序内部出错, 详情见日志.")
            return
        if summary["ok"] > 0 and messagebox.askyesno(APP_TITLE, text + "\n\n现在打开输出目录看看吗？"):
            self._open_outdir()

    # 关闭
    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "还在保存中, 确定要退出吗？"):
                return
            self.stop_event.set()
        self._save_settings()
        if self._log_handle:
            try:
                self._log_handle.close()
            except OSError:
                pass
            self._log_handle = None
        self.root.destroy()

# 入口
def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

def write_crash_log(text: str) -> Path:
    path = app_dir() / ERROR_LOG
    try:
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass
    return path

def main() -> int:
    enable_dpi_awareness()
    try:
        os.chdir(app_dir())  # 让相对路径 / cookie.txt 都以程序所在目录为准
    except OSError:
        pass
    root = tk.Tk()
    try:
        root.tk.call("tk", "scaling", root.winfo_fpixels("1i") / 72.0)
    except Exception:
        pass
    try:
        App(root)
    except Exception:
        detail = traceback.format_exc()
        path = write_crash_log(detail)
        try:
            messagebox.showerror(APP_TITLE, "界面初始化失败: \n%s\n\n详情已写入: \n%s" % (detail.splitlines()[-1], path))
        except Exception:
            pass
        return 1
    root.mainloop()
    return 0

# 自检
class _FakeHttp:
    """自检用: 不联网, 返回一篇文章 + 一张内联图片."""
    def __init__(self, page: str) -> None:
        self.page = page
        self.cookie = ""

    def get(self, url, extra_headers=None, allow_status=()):
        if "zhimg.com" in url:
            raise core.ZhihuError("自检不该请求网络图片")
        return 200, {"Content-Type": "text/html; charset=utf-8"}, self.page.encode("utf-8")

def _say(text: str) -> None:
    """窗口版 exe 里 stdout 可能是 None, 打印不能把程序弄崩."""
    try:
        print(text)
    except Exception:
        pass

def self_test() -> int:
    png = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
    content = (
        "<h2>自检小标题</h2>"
        '<p>这是一段<strong>加粗</strong>文字, 公式 <img class="eeimg" eeimg="1" '
        'src="https://www.zhihu.com/equation?tex=E%3Dmc%5E2" alt="E=mc^2"/>。</p>'
        '<p><img src="data:image/png;base64,' + png + '" alt="内联图"/></p>'
        '<pre lang="python"><code>print(&quot;hello&quot;)</code></pre>'
    )
    article = {"id": "1234567890", "title": "自检文章", "content": content,
               "author": {"name": "自检作者", "url_token": "selftest"}}
    state = {"initialState": {"entities": {"articles": {"1234567890": article}}}}
    page = ('<html><body><script id="js-initialData" type="text/json">%s</script></body></html>'
            % json.dumps(state, ensure_ascii=False))

    original_http = core.HttpClient
    core.HttpClient = lambda **kwargs: _FakeHttp(page)  # type: ignore[assignment]
    tmpdir = app_dir() / ".selftest-tmp"
    result_file = app_dir() / "selftest-result.txt"
    lines: List[str] = []

    class _Emit:
        def log(self, text: str) -> None:
            lines.append(text)

        def progress(self, index: int, total: int) -> None:
            lines.append("[进度 %d/%d]\n" % (index, total))

    try:
        if tmpdir.exists():
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)
        tmpdir.mkdir(parents=True, exist_ok=True)
        params = JobParams(text="p/1234567890", outdir=tmpdir, with_images=True, delay=0)
        summary = run_job(params, _Emit(), lambda: False)  # type: ignore[arg-type]
        files = list(tmpdir.glob("*.md"))
        assert summary["ok"] == 1, "should save 1 doc: %r" % summary
        assert len(files) == 1, "should create 1 md: %r" % files
        md = files[0].read_text(encoding="utf-8")
        assert "# 自检文章" in md, "missing title heading"
        assert "**加粗**" in md, "missing bold"
        assert "$E=mc^2$" in md, "missing latex"
        assert "```python" in md, "missing code fence"
        assert "自检作者" in md, "missing author"
        images = list(tmpdir.joinpath("assets").rglob("img-*"))
        assert len(images) == 1, "should download 1 image: %r" % images
        assert "assets/" in md, "image link should be relative"
        message = "SELF-TEST OK  (%s)\n  images: %s\n" % (
            files[0].name, [str(p.relative_to(tmpdir)) for p in images])
        _say(message.rstrip())
        result_file.write_text(message, encoding="utf-8")
        return 0
    except AssertionError as exc:
        message = "SELF-TEST FAILED: %s\n---- log ----\n%s" % (exc, "".join(lines))
        _say(message)
        try:
            result_file.write_text(message, encoding="utf-8")
        except OSError:
            pass
        return 1
    finally:
        core.HttpClient = original_http  # type: ignore[assignment]
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)

def smoke_ui() -> int:
    """验证 tkinter / tcl-tk 在打包后能正常初始化 (窗口不会显示)."""
    result_file = app_dir() / "smoke-ui-result.txt"
    try:
        enable_dpi_awareness()
        root = tk.Tk()
        root.withdraw()
        app = App(root)
        root.update_idletasks()
        status = app.var_status.get()
        outdir = app.var_outdir.get()
        root.destroy()
        message = "SMOKE-UI OK\n  status=%r\n  outdir=%r\n" % (status, outdir)
        _say(message.rstrip())
        result_file.write_text(message, encoding="utf-8")
        return 0
    except Exception:
        detail = traceback.format_exc()
        _say("SMOKE-UI FAILED\n" + detail)
        try:
            result_file.write_text("SMOKE-UI FAILED\n" + detail, encoding="utf-8")
        except OSError:
            pass
        return 1

if __name__ == "__main__":
    if "--self-test" in sys.argv:
        raise SystemExit(self_test())
    if "--smoke-ui" in sys.argv:
        raise SystemExit(smoke_ui())
    raise SystemExit(main())
