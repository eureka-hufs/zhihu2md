# zhihu2md —— 把知乎文章 / 回答备份成本地 Markdown

一个把知乎**专栏文章**和**回答**保存成本地 Markdown 的小工具：正文、图片、代码块、LaTeX 公式一起搬下来，产出的 `.md` 可以直接导入 **Obsidian**。

- **图形界面版**：`知乎备份.exe`，单文件，**目标电脑不需要安装 Python**（Windows 10/11 64 位）
- **命令行版**：`python zhihu2md.py p/123456`
- **零第三方依赖**：源码模式只用 Python 标准库（3.8+）
- 图片全部下载到本地 `assets/`，正文链接改写成相对路径 —— 原作者删号、知乎失效，本地内容依然完整
- 知乎把公式渲染成图片，这里会**还原成 `$...$` / `$$...$$` 的 LaTeX**（在 Obsidian 里可搜索、可编辑）
- 带 YAML front matter（`title / author / source / zhihu_id / created / tags`），Obsidian 属性面板可直接筛选

## 下载 / 使用

### 方式一：直接用 exe（推荐给大多数人）

到 [Releases](../../releases) 下载 `zhihu2md-gui-*-win64.exe`，双击运行。

- 首次运行若被 Windows SmartScreen 拦：点 **更多信息 → 仍要运行**（程序未做代码签名）
- 单文件程序启动会先解包到临时目录，**首次启动约 1~2 秒**属正常
- 只支持 Windows 10/11 **64 位**；Mac / Linux 请用命令行版

### 方式二：跑源码

```bash
git clone https://github.com/<你的用户名>/zhihu2md.git
cd zhihu2md
python zhihu2md.py p/2094715383932573257                  # 命令行
python zhihu2md_gui.py                                     # 图形界面
```

需要 Python 3.8+，不需要 `pip install` 任何东西。打包 exe 才需要 `pip install pyinstaller`。

## 必须先准备 Cookie

知乎对没有登录态的请求会直接返回 `403`，所以第一次用要先准备一份 Cookie。任选一种：

**A. 一个值（最快）**

1. 浏览器登录知乎，`F12` → **应用程序（Application）** → `Cookie → https://www.zhihu.com`
2. 双击 `z_c0` 的「值」→ `Ctrl+A` / `Ctrl+C`
3. 程序里点「粘贴识别」→「保存 Cookie」

**B. Copy as cURL**

1. `F12` → **网络（Network）** → `F5` 刷新 → 点列表里一条真正的 `www.zhihu.com/api/...` 请求
   （**别点 `blob:` 开头的**，那不是网络请求，没有 Cookie）
2. 右键该请求 → **复制 → 复制为 cURL**
3. 程序里点「粘贴识别」→「保存 Cookie」

工具会自动识别这些格式：裸 Cookie 串、整段请求头里的 `cookie:` 行、cURL 命令、DevTools Cookie 表格的两列粘贴、单独的 `z_c0` 值、Netscape `cookies.txt`、浏览器扩展导出的 JSON。

> Cookie 里**必须有 `z_c0`**。程序启动前会做一次体检，缺 `z_c0` 会直接提示，不会让你白等到 403。
> Cookie 只在本机使用，程序不会上传到任何地方；它通常几个月才过期。

## 输出长什么样

```
输出目录/
├── Rust 异步编程入门：从 Future 到 Pin.md
└── assets/
    └── Rust_异步编程入门：从_Future_到_Pin/
        ├── img-001.png
        └── img-002.gif
```

Markdown 开头是 front matter：

```markdown
---
title: "Rust 异步编程入门：从 Future 到 Pin"
author: "张三"
author_url: "https://www.zhihu.com/people/zhangsan-42"
source: "https://zhuanlan.zhihu.com/p/2094715383932573257"
zhihu_id: "2094715383932573257"
zhihu_type: "article"
created: "2023-11-15T06:13:20"
updated: "2023-11-15T07:13:20"
voteup: "1,234"
comments: "56"
saved_at: "2025-01-01T12:00:00"
tags:
  - zhihu
  - 文章
---
```

正文、代码块、公式、表格都是标准 Markdown：

````markdown
## 为什么需要异步

在 Rust 里，异步是一种**零成本抽象**：编译器把 `async fn` 展开成状态机。

行内公式：时间复杂度 $O(1)$。

```rust
async fn main() {
    println!("hello");
}
```

$$
\int_0^1 x^2 \, dx = \frac{1}{3}
$$
````

## 支持的输入格式

| 形式 | 例子 |
| --- | --- |
| 文章编号 | `p/2094715383932573257` |
| 回答编号 | `question/1914329673120347774/answer/2024747872659285839` |
| 只有回答 ID | `answer/2024747872659285839` |
| 纯数字（自动判断文章/回答） | `2094715383932573257` |
| 完整链接 | `https://zhuanlan.zhihu.com/p/...`、`https://www.zhihu.com/question/.../answer/...` |

批量：命令行 `-f links.txt`，或把收藏夹页面全选复制、整段粘进界面（会自动提取链接并去重）。

## 内容是怎么迁移的

| 知乎里的元素 | 保存成 |
| --- | --- |
| 标题 / 加粗 / 斜体 / 删除线 | `##` / `**` / `*` / `~~` |
| 代码块 | 带语言标记的围栏代码块，内容原样保留 |
| 行内 / 整段公式（`equation?tex=` 图片） | `$...$` / `$$...$$` |
| 图片（含懒加载、GIF、`data:` 内联图） | 下载到 `assets/<文章名>/img-00N.ext`，正文写相对路径 |
| 图注 | 图片下方 `*图注*` |
| 表格 | GFM 管道表格 |
| 引用 / 列表 / 分割线 | `> ` / `- `、`1. ` / `---` |
| 上下标、`<u>`、`<mark>` | 保留为 HTML 内联标签（Obsidian 支持） |

## 工作原理

1. **抓取（三层兜底）**
   - 优先解析页面里内嵌的 `<script id="js-initialData">` 的 `initialState` JSON（知乎自己 SSR 用的数据，字段最全）；
   - 页面结构变了就退回**解析 DOM**，从 `RichText` 容器里抠正文；
   - 最后兜底调用 `api/v4/articles|answers`，并尽力生成 `x-zse-96` 签名。
2. **转换**：自带 HTML 解析器（`html.parser` + 树结构）做 HTML → Markdown 语义转换，
   对知乎的公式图片、懒加载图片、高亮代码块分别做了专门处理。
3. **落盘**：图片并发下载 → 改写相对路径 → 写 front matter、来源信息、正文。

## 自己打包 exe

```bash
pip install pyinstaller

python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name zhihu2md-gui --icon zhihu2md.ico --add-data "zhihu2md.ico;." ^
  --distpath dist --workpath build . zhihu2md_gui.py
```

> 注意 `--add-data` 里的图标要写**绝对路径**，否则配合 `--specpath` 会找不到文件。

打包后的自检（不联网、不弹窗，结果写到程序目录）：

```bash
zhihu2md-gui.exe --self-test    # 验证 抓取 → 转换 → 下载图片 → 落盘 整条链路
zhihu2md-gui.exe --smoke-ui     # 验证 tkinter / tcl-tk 是否被完整打进 exe
```

换图标：`python tools/make_icon.py --source 你的图.png`（自动补正方形、生成多尺寸 ICO）。

## 测试

```bash
python -m unittest discover -s tests -v     # 78 个用例，完全离线
python tests/demo_offline.py                # 用仿真页面快照跑一遍，产物写到 samples/
```

覆盖：编号解析、HTML 树解析（嵌套列表 / 未闭合标签）、代码块 / 公式 / 表格 / 引用 / 图片转换、
Cookie 多格式解析与体检、文件名清洗、重复保存幂等、重名加后缀、图片下载失败回退、`--no-images` 模式等。

## 常见问题

**Q：报 403 / 提示需要 Cookie？**
登录态没带上或已过期。按上面的方法重新取 Cookie；若已带 Cookie 仍 403，通常是触发了安全验证，
用浏览器打开一次目标页面完成验证，再稍等一会儿重试。

**Q：图片没下载下来？**
个别图片有防盗链，程序会带 `Referer` 重试；仍失败的会退化成远程链接（正文不缺位置），
`gui-log.txt` / 控制台会指出是哪张失败。

**Q：付费内容 / 盐选专栏能保存吗？**
只能保存登录后能正常看到的部分，程序不会绕过付费墙。

**Q：知乎改版导致抓不到？**
加 `--debug`（命令行）会保存原始 HTML 到 `_debug/`，据此可适配解析逻辑；程序日志在 `gui-log.txt`。

## 免责声明

- 本工具仅供**个人备份**学习使用，请勿用于批量转载、传播他人作品；
- 抓取的内容版权归原作者所有，请遵守知乎的用户协议与相关法律法规；
- 因使用本工具产生的任何后果由使用者自行承担。

## 参考与致谢

思路上有参考这些现成方案（代码为本项目自行实现）：

- [chenluda/zhihu-download](https://github.com/chenluda/zhihu-download)
- [Jekeer/zhihu-copy-as-markdown](https://github.com/Jekeer/zhihu-copy-as-markdown)
- [qtqz/zhihu-backup-collect](https://github.com/qtqz/zhihu-backup-collect)
- [cv-cat/ZhihuApis](https://github.com/cv-cat/ZhihuApis)

## License

[MIT](LICENSE)
