# Search Books Skill / 搜书 Skills

[中文](#中文) · [English](#english)

An Agent Skill that searches one fixed Z-Library route by exact book title,
accepts only original PDF records, validates the downloaded bytes, and saves
the file into an Obsidian vault.

一个按精确书名搜索固定 Z-Library 路径、只接受原始 PDF、校验文件内容并保存到
Obsidian Vault 的 Agent Skill。

> [!IMPORTANT]
> Use this project only for public-domain works or content you own or are
> authorized to access. Comply with applicable copyright law and website terms.
> This project must not be used as an unattended or scheduled downloader.
>
> 本项目仅用于公版内容，或你拥有版权、已获得授权访问的内容。请遵守适用的版权法律和
> 网站条款。禁止把本项目作为无人值守或定时批量下载工具。

## 中文

### 项目功能

- 接收一个完整书名并执行精确匹配。
- 入口固定为 `https://z-library.bz/`，搜索与详情路径固定为入口页面声明的
  `https://z-library.im/` 路由，不扩展到其他镜像或搜索引擎。
- 只选择来源格式明确标记为 PDF 的记录，不接受 EPUB 转 PDF 或其他转换结果。
- 只保存未经修改的 PDF 字节，不生成 Markdown、摘要或其他附属文件。
- 将文件保存到 Obsidian Vault 的
  `raw/sources/hermes-inbox/books/<书名>.pdf`。
- 对 PDF 进行文件头、结束标记、大小和 SHA-256 校验。
- 按 SHA-256 去重，发现同名不同内容时拒绝覆盖。
- 提供 `doctor`、`login` 和 `download` 三个命令，并支持 JSON 输出。

### 实现原理

```mermaid
flowchart LR
    A["用户提供一个书名"] --> B["doctor 运行前检查"]
    B --> C["固定入口 z-library.bz"]
    C --> D["受控搜索 z-library.im"]
    D --> E["精确标题 + 原始 PDF"]
    E --> F["验证过的 /dl/ 文件地址"]
    F --> G["HTTPS 下载且不泄露 Cookie"]
    G --> H["PDF 内容与 SHA-256 校验"]
    H --> I["去重并原子写入 Obsidian"]
```

核心防护机制：

1. **运行前闸门**：检查 Playwright CLI、Obsidian Vault、DNS、入口页面和搜索
   路由。任一关键检查失败就停止。
2. **来源限制**：浏览器页面只允许进入两个固定来源；DNS 解析会拒绝回环地址、
   私网地址和不符合规则的目标。
3. **精确选择**：规范化书名后要求完全一致，并检查记录原始扩展名为 `pdf`；
   `convertedTo=pdf` 等转换链接会被拒绝。
4. **下载隔离**：只有经 `z-library.im/dl/` 页面证明的 HTTPS 文件地址才可进入
   下载阶段。外部文件主机不会收到 Z-Library Cookie，也不允许继续重定向。
5. **文件验证**：检查 `%PDF-` 文件头、`%%EOF` 结束标记、最小文件大小和
   1 GB 上限，然后计算 SHA-256。
6. **不可覆盖写入**：先写临时文件并重新核对哈希，再通过原子文件操作提交；
   相同哈希返回 `duplicate`，同名不同内容返回 `conflict`。
7. **会话保护**：登录信息只保存在本机权限受限的 Hermes 状态目录中，不在聊天
   中接收密码，也不进入 Git 仓库。

### 环境要求

- Python 3.10 或更高版本
- Node.js 与 npm/npx
- 固定版本 `@playwright/cli@0.1.17`
- 系统 `curl`
- 一个已存在且包含 `.obsidian/` 的 Obsidian Vault
- 已在 macOS 上验证；其他 POSIX 系统需要自行验证

安装 Playwright CLI：

```bash
npm install -g @playwright/cli@0.1.17
```

### 安装到 Hermes

```bash
git clone https://github.com/zhangboshuo5-prog/search-books-skill.git
cd search-books-skill

# 默认 Hermes Home；使用 profile 时请改成对应目录，例如 ~/.hermes/profiles/s
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills/research"
cp -R search-books "$HERMES_HOME/skills/research/search-books"

hermes skills list
```

### 安装到 Codex

全局安装会让所有 Codex 项目都能发现这个 Skill：

```bash
git clone https://github.com/zhangboshuo5-prog/search-books-skill.git
cd search-books-skill

mkdir -p "$HOME/.agents/skills"
cp -R search-books "$HOME/.agents/skills/search-books"

export SEARCH_BOOKS_STATE_DIR="$HOME/.codex/state/search-books"
```

如果只想让一个项目使用，将 Skill 放到该项目的 `.agents/skills/`：

```bash
export PROJECT_ROOT="/你的/Git/仓库/绝对路径"
mkdir -p "$PROJECT_ROOT/.agents/skills"
cp -R search-books "$PROJECT_ROOT/.agents/skills/search-books"

export SEARCH_BOOKS_STATE_DIR="$HOME/.codex/state/search-books"
```

Codex 会从当前目录向上扫描到仓库根目录中的 `.agents/skills`，并同时读取用户级
`$HOME/.agents/skills`。它通常会自动检测新增 Skill；如果没有出现，请重启 Codex。
目标目录已经存在时不要直接覆盖；先备份并比较差异。

上述目录与调用方式依据 OpenAI 官方
[Codex Build skills](https://learn.chatgpt.com/docs/build-skills.md) 文档。

### 配置

```bash
export OBSIDIAN_VAULT_PATH="/你的/Obsidian/Vault/绝对路径"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
```

可选环境变量：

| 变量 | 用途 |
|---|---|
| `OBSIDIAN_VAULT_PATH` | 覆盖默认 Vault 路径 `~/Documents/wiki/My-wiki` |
| `HERMES_HOME` | 指定 Hermes 根目录或当前 profile 目录 |
| `SEARCH_BOOKS_STATE_DIR` | 指定登录状态目录；Codex 推荐 `~/.codex/state/search-books` |
| `PLAYWRIGHT_CLI_BIN` | 指定 `playwright-cli` 可执行文件的绝对路径 |

### 在 Codex 中使用

显式调用最可靠。在 Codex 桌面端可从侧边栏打开 **Skills** 查看；在 Codex
CLI/IDE 中可以运行 `/skills`，也可以直接输入：

```text
使用 $search-books 搜索《置身事内》的原始 PDF，并保存到我的 Obsidian Vault。
```

也可以自然表达：

```text
用搜书 Skills 下载《置身事内》，只保留原始 PDF。
```

Codex 会读取 `SKILL.md`，先运行 `doctor`，通过后再执行精确标题搜索。若需要登录，
Codex 应让你在本机交互式终端完成登录，不会在对话中索取密码。每次下载都必须由你
明确提供一个书名；不要把该 Skill 接入定时或批量任务。

### 使用方法

先检查环境：

```bash
python3 search-books/scripts/search_books.py doctor --json
```

如果目标书需要登录，在本机交互式终端执行一次：

```bash
python3 search-books/scripts/search_books.py login
```

按完整书名搜索并保存原始 PDF：

```bash
python3 search-books/scripts/search_books.py download "置身事内" --json
```

成功后仅生成一个 PDF：

```text
<Vault>/raw/sources/hermes-inbox/books/置身事内.pdf
```

`saved` 表示新文件已保存，`duplicate` 表示相同文件已经存在，`conflict` 表示同名
文件存在但内容不同。错误状态不会被描述为下载成功。

### 测试

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s search-books/tests -p 'test_*.py' -v
python3 -m py_compile search-books/scripts/search_books.py
```

自动化测试不执行真实图书下载，不写入真实 Obsidian Vault。

## English

### Features

- Accept one complete book title and require an exact normalized match.
- Enter through `https://z-library.bz/` and use only the
  `https://z-library.im/` search/detail route declared by that page.
- Accept records whose original source format is PDF; reject EPUB conversion
  and other generated-PDF paths.
- Save unchanged PDF bytes only—no Markdown, summary, or sidecar files.
- Write to
  `raw/sources/hermes-inbox/books/<title>.pdf` inside an Obsidian vault.
- Validate PDF structure, size, and SHA-256 before reporting success.
- Deduplicate by SHA-256 and never overwrite a different existing file.
- Provide `doctor`, `login`, and `download` commands with JSON output.

### How It Works

1. **Preflight gate** checks Playwright CLI, the Obsidian vault, DNS, the entry
   page, and the declared search route. Any critical failure stops the run.
2. **Origin controls** allow browser pages only on the two fixed origins. DNS
   checks reject loopback, private, and otherwise unsafe targets.
3. **Exact selection** normalizes the requested title, requires equality, and
   requires the record's original extension to be `pdf`. Conversion markers
   such as `convertedTo=pdf` are rejected.
4. **Isolated transfer** accepts an external HTTPS file URL only after it is
   attested by a `z-library.im/dl/` page. Site cookies are never forwarded to
   the external host, and further redirects are disabled.
5. **Content validation** checks the `%PDF-` header, the `%%EOF` marker, a
   minimum size, and a 1 GB cap before calculating SHA-256.
6. **Non-overwriting commit** copies to a temporary file, verifies the hash
   again, and commits atomically. Equal content returns `duplicate`; a
   different file with the same title returns `conflict`.
7. **Session isolation** stores browser state only in a permission-restricted
   local Hermes state directory. Passwords are never requested in chat and
   session files are excluded from Git.

### Requirements

- Python 3.10+
- Node.js with npm/npx
- Pinned `@playwright/cli@0.1.17`
- System `curl`
- An existing Obsidian vault containing `.obsidian/`
- Verified on macOS; other POSIX systems require independent validation

Install Playwright CLI:

```bash
npm install -g @playwright/cli@0.1.17
```

### Install for Hermes

```bash
git clone https://github.com/zhangboshuo5-prog/search-books-skill.git
cd search-books-skill

# Change this when using a profile, for example ~/.hermes/profiles/s
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
mkdir -p "$HERMES_HOME/skills/research"
cp -R search-books "$HERMES_HOME/skills/research/search-books"

hermes skills list
```

### Install for Codex

A global install makes the Skill discoverable from every Codex project:

```bash
git clone https://github.com/zhangboshuo5-prog/search-books-skill.git
cd search-books-skill

mkdir -p "$HOME/.agents/skills"
cp -R search-books "$HOME/.agents/skills/search-books"

export SEARCH_BOOKS_STATE_DIR="$HOME/.codex/state/search-books"
```

For a project-local install, place the Skill under that project's
`.agents/skills/` directory:

```bash
export PROJECT_ROOT="/absolute/path/to/your/git/repository"
mkdir -p "$PROJECT_ROOT/.agents/skills"
cp -R search-books "$PROJECT_ROOT/.agents/skills/search-books"

export SEARCH_BOOKS_STATE_DIR="$HOME/.codex/state/search-books"
```

Codex scans `.agents/skills` from the current working directory up to the
repository root and also reads the user-level `$HOME/.agents/skills`. It
normally detects new Skills automatically; restart Codex if the Skill does not
appear. If the destination already exists, back it up and inspect the diff
instead of overwriting it blindly.

These locations and invocation methods follow OpenAI's official
[Codex Build skills](https://learn.chatgpt.com/docs/build-skills.md)
documentation.

### Configuration

```bash
export OBSIDIAN_VAULT_PATH="/absolute/path/to/your/Obsidian/Vault"
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
```

Optional environment variables:

| Variable | Purpose |
|---|---|
| `OBSIDIAN_VAULT_PATH` | Override the default `~/Documents/wiki/My-wiki` vault |
| `HERMES_HOME` | Select the Hermes root or active profile directory |
| `SEARCH_BOOKS_STATE_DIR` | Select session storage; Codex should use `~/.codex/state/search-books` |
| `PLAYWRIGHT_CLI_BIN` | Select an absolute `playwright-cli` executable path |

### Use with Codex

Explicit invocation is the most reliable. In the Codex desktop app, open
**Skills** in the sidebar. In Codex CLI/IDE, run `/skills` to inspect installed
Skills, or enter:

```text
Use $search-books to find the authorized original PDF for "Book Title" and
save it to my Obsidian vault.
```

Natural-language invocation also works when the title and original-PDF intent
are explicit:

```text
Use Search Books to download "Book Title" and keep only the original PDF.
```

Codex reads `SKILL.md`, runs `doctor`, and proceeds only after the preflight
passes. If authentication is required, Codex must ask you to complete login in
a local interactive terminal and must never request a password in chat. Every
download requires one explicit title; do not wire this Skill into scheduled or
batch tasks.

### Usage

Run the preflight check:

```bash
python3 search-books/scripts/search_books.py doctor --json
```

If a book requires authentication, run the one-time interactive login locally:

```bash
python3 search-books/scripts/search_books.py login
```

Search by exact title and save the original PDF:

```bash
python3 search-books/scripts/search_books.py download "Book Title" --json
```

A successful run creates only one file:

```text
<Vault>/raw/sources/hermes-inbox/books/Book Title.pdf
```

`saved` means a new file was committed, `duplicate` means identical content
already exists, and `conflict` means the title path is owned by different
content. An error status must never be reported as a successful download.

### Testing

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover \
  -s search-books/tests -p 'test_*.py' -v
python3 -m py_compile search-books/scripts/search_books.py
```

The automated test suite does not download real books or write to a real vault.

## Repository Layout

```text
search-books-skill/
├── README.md
├── LICENSE
├── NOTICE
├── search-books/
│   ├── SKILL.md
│   ├── agents/openai.yaml
│   ├── scripts/search_books.py
│   └── tests/test_search_books.py
└── .github/workflows/ci.yml
```

## Attribution and License

This project is inspired by
[`zstmfhy/zlibrary-to-notebooklm`](https://github.com/zstmfhy/zlibrary-to-notebooklm),
which is distributed under the MIT License. This implementation removes
NotebookLM, format conversion, and batch behavior, and adds exact-title search,
strict origin controls, PDF validation, deduplication, and non-overwriting
Obsidian writes.

Released under the MIT License. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
