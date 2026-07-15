#!/usr/bin/env python3
"""Search one fixed Z-Library site and save an original PDF to Obsidian.

The workflow is inspired by zstmfhy/zlibrary-to-notebooklm (MIT), but this
implementation removes NotebookLM and EPUB conversion, adds title search,
strict PDF validation, deduplication, and non-overwriting Vault writes.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Iterable, Sequence


ENTRY_BASE_URL = "https://z-library.bz/"
SEARCH_BASE_URL = "https://z-library.im/"
BASE_URL = ENTRY_BASE_URL
ENTRY_HOST = "z-library.bz"
SEARCH_HOST = "z-library.im"
ALLOWED_SITE_HOSTS = frozenset({ENTRY_HOST, SEARCH_HOST})
ALLOWED_SITE_ORIGINS = frozenset(
    {ENTRY_BASE_URL.rstrip("/"), SEARCH_BASE_URL.rstrip("/")}
)
DEFAULT_VAULT_PATH = Path.home() / "Documents" / "wiki" / "My-wiki"
DEFAULT_HERMES_HOME = Path.home() / ".hermes"
BOOKS_SUBDIR = Path("raw/sources/hermes-inbox/books")
STATE_DIR_NAME = "search-books"
STORAGE_STATE_NAME = "storage-state-z-library.json"
MIN_PDF_BYTES = 512
MAX_DOWNLOAD_BYTES = 1_000_000_000
HTTP_TIMEOUT_SECONDS = 45
BROWSER_TIMEOUT_SECONDS = 180
PLAYWRIGHT_CLI_VERSION = "0.1.17"
ALLOWED_PROXY_FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"),)
REQUIRED_AUTH_COOKIES = frozenset({"remix_userid", "remix_userkey"})
OUTER_TITLE_MARKS = (
    ("《", "》"),
    ("〈", "〉"),
    ("「", "」"),
    ("『", "』"),
    ("“", "”"),
    ('"', '"'),
    ("'", "'"),
)


class SkillError(RuntimeError):
    """Expected, user-actionable skill failure."""

    def __init__(self, message: str, *, code: int = 2, details: Any = None):
        super().__init__(message)
        self.code = code
        self.details = details


def normalize_title(value: str) -> str:
    """Normalize case/spacing and outer title marks while preserving semantics."""

    normalized = unicodedata.normalize("NFKC", value or "").strip()
    changed = True
    while changed and len(normalized) >= 2:
        changed = False
        for left, right in OUTER_TITLE_MARKS:
            if normalized.startswith(left) and normalized.endswith(right):
                normalized = normalized[len(left) : len(normalized) - len(right)].strip()
                changed = True
                break
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.lower()


def sanitize_filename_title(value: str, *, max_length: int = 120) -> str:
    """Create a readable, path-safe filename stem without transliteration."""

    value = unicodedata.normalize("NFKC", value or "")
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Cc")
    value = re.sub(r"[\\/:*?\"<>|]+", "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .-")
    if not value:
        raise SkillError("书名清理后为空，无法生成安全文件名。")
    if len(value) > max_length:
        value = value[:max_length].rstrip(" .-")
    return value


def default_state_dir() -> Path:
    skill_state_dir = os.environ.get("SEARCH_BOOKS_STATE_DIR")
    if skill_state_dir:
        return Path(skill_state_dir).expanduser()
    hermes_home = os.environ.get("HERMES_HOME")
    root = Path(hermes_home).expanduser() if hermes_home else DEFAULT_HERMES_HOME
    return root / "state" / STATE_DIR_NAME


def resolve_state_dir(value: str | None) -> Path:
    path = Path(value).expanduser() if value else default_state_dir()
    path.mkdir(parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
        if path.stat().st_mode & 0o077:
            raise SkillError(f"状态目录权限过宽：{path}")
    except OSError as exc:
        raise SkillError(f"无法保护 Hermes 状态目录：{path}: {exc}") from exc
    return path.resolve()


def resolve_vault(value: str | None) -> Path:
    raw = value or os.environ.get("OBSIDIAN_VAULT_PATH") or str(DEFAULT_VAULT_PATH)
    vault = Path(raw).expanduser().resolve()
    if not vault.is_dir():
        raise SkillError(f"Obsidian Vault 不存在：{vault}")
    if not (vault / ".obsidian").is_dir():
        raise SkillError(f"目标目录不是可识别的 Obsidian Vault：{vault}")
    return vault


def books_directory(vault: Path) -> Path:
    vault = vault.resolve()
    target = (vault / BOOKS_SUBDIR).resolve()
    try:
        target.relative_to(vault)
    except ValueError as exc:
        raise SkillError("目标书籍目录越出了 Obsidian Vault。") from exc
    target.mkdir(parents=True, exist_ok=True)
    return target


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_pdf(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise SkillError("下载结果不存在。", code=5)
    size = path.stat().st_size
    if size < MIN_PDF_BYTES:
        raise SkillError(f"下载结果过小，不能视为 PDF：{size} bytes。", code=5)
    with path.open("rb") as handle:
        prefix = handle.read(1024)
        handle.seek(max(0, size - 4096))
        suffix = handle.read(4096)
    if not prefix.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").startswith(b"%PDF-"):
        raise SkillError("下载结果未通过 PDF 文件头校验。", code=5)
    if b"%%EOF" not in suffix:
        raise SkillError("下载结果缺少 PDF 结束标记。", code=5)
    return {"size": size, "sha256": hash_file(path)}


def commit_pdf(source: Path, vault: Path, title: str) -> dict[str, Any]:
    """Atomically add a validated PDF without replacing an existing file."""

    metadata = validate_pdf(source)
    directory = books_directory(vault)

    for existing in directory.glob("*.pdf"):
        if existing.is_symlink():
            continue
        if existing.is_file() and hash_file(existing) == metadata["sha256"]:
            source.unlink(missing_ok=True)
            return {
                "status": "duplicate",
                "path": str(existing.resolve()),
                **metadata,
            }

    destination = directory / f"{sanitize_filename_title(title)}.pdf"
    if destination.exists() or destination.is_symlink():
        source.unlink(missing_ok=True)
        return {
            "status": "conflict",
            "path": str(destination.resolve()),
            "error": "同名 PDF 已存在且内容哈希不同；未覆盖。",
            **metadata,
        }

    partial = directory / f".{destination.name}.{uuid.uuid4().hex}.partial"
    try:
        with source.open("rb") as src, partial.open("xb") as dst:
            shutil.copyfileobj(src, dst, length=1024 * 1024)
            dst.flush()
            os.fsync(dst.fileno())
        if hash_file(partial) != metadata["sha256"]:
            raise SkillError("写入 Vault 前后的 PDF 哈希不一致。", code=5)
        try:
            os.link(partial, destination)
        except FileExistsError:
            return {
                "status": "conflict",
                "path": str(destination.resolve()),
                "error": "写入时检测到同名文件；未覆盖。",
                **metadata,
            }
        except OSError as exc:
            raise SkillError(f"无法原子写入 Obsidian Vault：{exc}", code=5) from exc
        try:
            destination.chmod(0o644)
        except OSError:
            pass
    finally:
        partial.unlink(missing_ok=True)
        source.unlink(missing_ok=True)

    return {"status": "saved", "path": str(destination.resolve()), **metadata}


def resolve_playwright_cli() -> list[str]:
    override = os.environ.get("PLAYWRIGHT_CLI_BIN")
    if override:
        path = Path(override).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SkillError(f"PLAYWRIGHT_CLI_BIN 不可执行：{path}")
        return [str(path)]

    global_cli = shutil.which("playwright-cli")
    if global_cli:
        return [global_cli]

    cached = sorted(
        (Path.home() / ".npm" / "_npx").glob("*/node_modules/.bin/playwright-cli"),
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=True,
    )
    for candidate in cached:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]

    raise SkillError(
        "未找到 Playwright CLI；请手动安装固定版本 "
        f"@playwright/cli@{PLAYWRIGHT_CLI_VERSION}。"
    )


def run_cli(
    cli: Sequence[str],
    session: str,
    args: Sequence[str],
    *,
    json_output: bool = False,
    timeout: int = BROWSER_TIMEOUT_SECONDS,
) -> str:
    command = [*cli]
    if json_output:
        command.append("--json")
    command.extend([f"-s={session}", *args])
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SkillError("Playwright 操作超时。", code=3) from exc
    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
    if completed.returncode != 0 or "### Error" in output:
        tail = output[-2000:] if output else f"exit={completed.returncode}"
        raise SkillError(f"Playwright 操作失败：{tail}", code=3)
    return completed.stdout.strip()


def close_session(cli: Sequence[str], session: str) -> None:
    try:
        run_cli(cli, session, ["close"], timeout=30)
    except SkillError:
        pass


def cookie_header(storage_state: Path) -> tuple[str, dict[str, str]]:
    if storage_state.is_symlink():
        raise SkillError(f"拒绝读取符号链接形式的登录状态：{storage_state}")
    try:
        stat = storage_state.stat()
    except OSError as exc:
        raise SkillError(f"无法读取登录状态属性：{storage_state}") from exc
    if hasattr(os, "getuid") and stat.st_uid != os.getuid():
        raise SkillError("登录状态文件不属于当前用户。")
    if stat.st_mode & 0o077:
        raise SkillError("登录状态文件权限过宽；必须为 0600。")
    try:
        data = json.loads(storage_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillError(f"无法读取登录状态：{storage_state}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("cookies", []), list):
        raise SkillError("登录状态 JSON 结构无效。")

    pairs: list[str] = []
    values: dict[str, str] = {}
    for cookie in data.get("cookies", []):
        if not isinstance(cookie, dict):
            continue
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if domain != SEARCH_HOST:
            continue
        try:
            expires = float(cookie.get("expires", -1))
        except (TypeError, ValueError):
            expires = -1
        if expires > 0 and expires <= time.time():
            continue
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if name:
            pairs.append(f"{name}={value}")
            values[name] = value
    return "; ".join(pairs), values


def has_authenticated_session(values: dict[str, str]) -> bool:
    normalized = {
        str(name).casefold().replace("-", "_"): str(value)
        for name, value in values.items()
        if value
    }
    return all(normalized.get(name) for name in REQUIRED_AUTH_COOKIES)


def multipart_body(fields: dict[str, str]) -> tuple[bytes, str]:
    boundary = f"----HermesZlib{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def request_json(
    url: str,
    *,
    cookie: str,
    data: bytes | None = None,
    content_type: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    validate_site_url(url, purpose="API")
    validate_download_url(url)
    headers = {
        "Accept": "application/json, text/plain, */*",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Referer": BASE_URL,
    }
    if cookie:
        headers["Cookie"] = cookie
    if content_type:
        headers["Content-Type"] = content_type
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, data=data, headers=headers)
    opener = urllib.request.build_opener(SafeRedirectHandler())
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(10 * 1024 * 1024 + 1)
    except SkillError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SkillError(f"站内 API 请求失败：{exc}", code=3) from exc
    if len(raw) > 10 * 1024 * 1024:
        raise SkillError("站内 API 响应异常大，已拒绝解析。", code=3)
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillError("站内 API 未返回有效 JSON。", code=3) from exc
    if not isinstance(parsed, dict):
        raise SkillError("站内 API 返回结构异常。", code=3)
    return parsed


def books_list_from_payload(payload: dict[str, Any]) -> list[Any] | None:
    candidates: Any = payload.get("books")
    if not isinstance(candidates, list):
        for key in ("result", "data"):
            nested = payload.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("books"), list):
                candidates = nested["books"]
                break
    if not isinstance(candidates, list):
        return None
    return candidates


def has_books_schema(payload: dict[str, Any]) -> bool:
    return books_list_from_payload(payload) is not None


def extract_books(payload: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = books_list_from_payload(payload) or []
    return [item for item in candidates if isinstance(item, dict)]


def select_exact_pdf(books: Iterable[dict[str, Any]], title: str) -> dict[str, Any] | None:
    requested = normalize_title(title)
    if not requested:
        return None
    for book in books:
        if normalize_title(str(book.get("title", ""))) != requested:
            continue
        if str(book.get("extension", "")).strip().casefold() != "pdf":
            continue
        return book
    return None


def api_search(title: str, storage_state: Path) -> tuple[dict[str, Any], str, dict[str, str]]:
    cookie, cookie_values = cookie_header(storage_state)
    if not has_authenticated_session(cookie_values):
        raise SkillError("登录状态缺少必要的 Z-Library 认证 Cookie。", code=3)
    body, boundary = multipart_body(
        {"q": title, "page": "1", "limit": "20", "order": "popular"}
    )
    payload = request_json(
        urllib.parse.urljoin(BASE_URL, "/api/search"),
        cookie=cookie,
        data=body,
        content_type=f"multipart/form-data; boundary={boundary}",
    )
    book = select_exact_pdf(extract_books(payload), title)
    if not book:
        raise SkillError("站内 API 未找到书名精确匹配的原始 PDF。", code=4)
    return book, cookie, cookie_values


def nested_download_link(payload: dict[str, Any]) -> str | None:
    for key in ("downloadLink", "download_link", "url", "dl"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    nested = payload.get("file")
    if isinstance(nested, dict):
        return nested_download_link(nested)
    return None


def api_download_link(
    book: dict[str, Any], cookie: str, cookie_values: dict[str, str]
) -> str:
    del cookie, cookie_values
    direct = book.get("dl")
    if isinstance(direct, str) and direct.strip():
        link = urllib.parse.urljoin(SEARCH_BASE_URL, direct.strip())
        validate_site_url(link, purpose="原始 PDF 直链")
        validate_original_pdf_url(link)
        return link
    raise SkillError(
        "新站搜索结果没有已验证的原始 PDF 直链；转入受控 UI 回退。",
        code=4,
    )


def validate_site_url(url: str, *, purpose: str) -> str:
    parsed = urllib.parse.urlparse(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SkillError(f"{purpose} 链接端口无效。", code=4) from exc
    if (
        parsed.scheme != "https"
        or (parsed.hostname or "").casefold() not in ALLOWED_SITE_HOSTS
        or port not in {None, 443}
    ):
        raise SkillError(f"拒绝访问固定来源之外的{purpose}链接。", code=4)
    return url


def validate_original_pdf_url(url: str) -> str:
    decoded = str(url)
    for _ in range(3):
        next_value = urllib.parse.unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    if "convertedto=pdf" in decoded.casefold():
        raise SkillError("拒绝使用转换生成的 PDF 链接。", code=4)
    return url


def validate_download_url(url: str, *, allow_proxy_fake_ip: bool = False) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SkillError("站内返回了无效的下载链接。", code=4)
    try:
        port = parsed.port
    except ValueError as exc:
        raise SkillError("下载链接端口无效。", code=4) from exc
    if port not in {None, 443}:
        raise SkillError("拒绝访问非标准 HTTPS 端口。", code=4)
    if parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}:
        raise SkillError("拒绝访问本机地址形式的下载链接。", code=4)
    try:
        addresses = socket.getaddrinfo(
            parsed.hostname,
            parsed.port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError as exc:
        raise SkillError(f"下载地址无法解析：{exc}", code=3) from exc
    hostname = parsed.hostname.casefold()
    for item in addresses:
        ip = ipaddress.ip_address(item[4][0])
        is_safe_public_ip = ip.is_global and not (
            ip.is_multicast
            or ip.is_unspecified
            or ip.is_reserved
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_private
        )
        if is_safe_public_ip:
            continue
        is_allowed_proxy_fake_ip = (
            hostname in ALLOWED_SITE_HOSTS or allow_proxy_fake_ip
        ) and any(
            ip in network for network in ALLOWED_PROXY_FAKE_IP_NETWORKS
        )
        if not is_allowed_proxy_fake_ip:
            raise SkillError("拒绝访问非公网下载地址。", code=4)
    return url


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Validate each HTTPS redirect and strip secrets when leaving the site."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        target = urllib.parse.urljoin(req.full_url, newurl)
        validate_original_pdf_url(target)
        validate_download_url(target)
        redirected = super().redirect_request(req, fp, code, msg, headers, target)
        if redirected is None:
            return None
        source = urllib.parse.urlparse(req.full_url)
        destination = urllib.parse.urlparse(target)
        source_origin = (source.scheme.casefold(), (source.hostname or "").casefold(), source.port)
        destination_origin = (
            destination.scheme.casefold(),
            (destination.hostname or "").casefold(),
            destination.port,
        )
        if source_origin != destination_origin:
            if (
                (source.hostname or "").casefold() != SEARCH_HOST
                or not source.path.startswith("/dl/")
            ):
                raise SkillError(
                    "拒绝非 z-library.im 下载入口触发的跨站重定向。", code=4
                )
            for name in ("Cookie", "Authorization", "Proxy-Authorization"):
                redirected.remove_header(name)
        return redirected


def download_headers(url: str, cookie: str) -> dict[str, str]:
    parsed = urllib.parse.urlparse(url)
    headers = {
        "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/126 Safari/537.36",
        "Referer": SEARCH_BASE_URL,
    }
    if cookie and (parsed.hostname or "").casefold() == SEARCH_HOST:
        headers["Cookie"] = cookie
    return headers


def validate_attested_download_url(url: str, source_url: str | None) -> str:
    if source_url is None:
        validate_site_url(url, purpose="PDF 下载")
    else:
        validate_site_url(source_url, purpose="PDF 下载入口")
        validate_original_pdf_url(source_url)
        source = urllib.parse.urlparse(source_url)
        if (
            (source.hostname or "").casefold() != SEARCH_HOST
            or not source.path.startswith("/dl/")
        ):
            raise SkillError("PDF 文件地址不是由 z-library.im/dl/ 入口产生。", code=4)
    validate_original_pdf_url(url)
    return validate_download_url(
        url, allow_proxy_fake_ip=source_url is not None
    )


def http_download(
    url: str,
    destination: Path,
    *,
    cookie: str,
    source_url: str | None = None,
) -> None:
    url = validate_attested_download_url(url, source_url)
    curl = shutil.which("curl")
    if not curl:
        raise SkillError("未找到系统 curl，无法使用系统证书链下载 PDF。", code=3)
    created_destination = False
    try:
        with destination.open("xb"):
            created_destination = True
        command = [
            curl,
            "--fail",
            "--silent",
            "--show-error",
            "--proto",
            "=https",
            "--location",
            "--max-redirs",
            "0",
            "--max-time",
            str(BROWSER_TIMEOUT_SECONDS),
            "--max-filesize",
            str(MAX_DOWNLOAD_BYTES),
            "--output",
            str(destination),
        ]
        for name, value in download_headers(url, cookie).items():
            command.extend(["--header", f"{name}: {value}"])
        command.extend(["--url", url])
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=BROWSER_TIMEOUT_SECONDS + 15,
        )
        if completed.returncode != 0:
            error = (completed.stderr or completed.stdout or "").strip()
            raise SkillError(
                f"PDF 下载失败：curl exit {completed.returncode}: {error[-1000:]}",
                code=3,
            )
        if destination.stat().st_size > MAX_DOWNLOAD_BYTES:
            raise SkillError("PDF 超过 1 GB 安全上限。", code=5)
    except SkillError:
        if created_destination:
            destination.unlink(missing_ok=True)
        raise
    except (subprocess.TimeoutExpired, OSError) as exc:
        if created_destination:
            destination.unlink(missing_ok=True)
        raise SkillError(f"PDF 下载失败：{exc}", code=3) from exc


def build_browser_fallback_code(title: str, debug_path: Path) -> str:
    config = json.dumps(
        {
            "entryBaseUrl": ENTRY_BASE_URL,
            "searchBaseUrl": SEARCH_BASE_URL,
            "allowedOrigins": sorted(ALLOWED_SITE_ORIGINS),
            "title": title,
            "debugPath": str(debug_path),
        },
        ensure_ascii=False,
    )
    return f"""async (page) => {{
  const cfg = {config};
  const allowedOrigins = new Set(cfg.allowedOrigins);
  let blockedNavigation = '';
  await page.context().addInitScript(() => {{
    try {{
      ServiceWorkerContainer.prototype.register = () =>
        Promise.reject(new Error('Service workers are disabled by Hermes'));
    }} catch {{}}
  }});
  const originOf = value => {{
    const match = String(value || '').match(/^(https?):\\/\\/([^/?#]+)/i);
    return match ? match[1].toLowerCase() + '://' + match[2].toLowerCase() : '';
  }};
  const entryOrigin = originOf(cfg.entryBaseUrl);
  const searchOrigin = originOf(cfg.searchBaseUrl);
  const pathnameOf = value => {{
    const match = String(value || '').match(/^https?:\\/\\/[^/?#]+([^?#]*)/i);
    return match && match[1] ? match[1] : '/';
  }};
  const isConvertedPdfUrl = value => {{
    let decoded = String(value || '');
    for (let i = 0; i < 3; i++) {{
      try {{
        const next = decodeURIComponent(decoded);
        if (next === decoded) break;
        decoded = next;
      }} catch {{ break; }}
    }}
    return decoded.toLowerCase().includes('convertedto=pdf');
  }};
  const normalize = value => {{
    let result = (value || '').normalize('NFKC').trim();
    const pairs = [['《', '》'], ['〈', '〉'], ['「', '」'], ['『', '』'],
      ['“', '”'], ['"', '"'], ["'", "'"]];
    let changed = true;
    while (changed && result.length >= 2) {{
      changed = false;
      for (const [left, right] of pairs) {{
        if (result.startsWith(left) && result.endsWith(right)) {{
          result = result.slice(left.length, result.length - right.length).trim();
          changed = true;
          break;
        }}
      }}
    }}
    return result.replace(/\\s+/g, ' ').toLocaleLowerCase();
  }};
  const assertCurrentOrigin = expected => {{
    const origin = originOf(page.url());
    if (!allowedOrigins.has(origin) || (expected && origin !== expected)) {{
      throw new Error(`页面跳转离开允许来源：${{origin}}`);
    }}
  }};

  await page.context().route('**/*', async route => {{
    const request = route.request();
    const origin = originOf(request.url());
    if (!allowedOrigins.has(origin)) {{
      if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {{
        blockedNavigation = request.url();
      }}
      await route.abort('blockedbyclient');
      return;
    }}
    await route.continue();
  }});

  const deepData = async () => page.evaluate(() => {{
    const textOf = node => {{
      if (!node) return '';
      let text = '';
      const stack = [node];
      const seen = new Set();
      while (stack.length) {{
        const current = stack.pop();
        if (!current || seen.has(current)) continue;
        seen.add(current);
        if (current.nodeType === Node.TEXT_NODE) text += ` ${{current.textContent || ''}}`;
        if (current.shadowRoot) stack.push(current.shadowRoot);
        if (current.childNodes) stack.push(...current.childNodes);
      }}
      return text.replace(/\\s+/g, ' ').trim();
    }};
    const anchors = [];
    const stack = [document];
    const seen = new Set();
    while (stack.length) {{
      const current = stack.pop();
      if (!current || seen.has(current)) continue;
      seen.add(current);
      if (current.nodeType === Node.ELEMENT_NODE && current.tagName === 'A') {{
        let scope = current;
        let card = null;
        const href = current.href || '';
        if (href.includes('/book/')) {{
          for (let i = 0; i < 10; i++) {{
            if (scope.tagName === 'Z-BOOKCARD') {{
              card = scope;
              break;
            }}
            const root = scope.getRootNode && scope.getRootNode();
            const next = scope.parentElement || (root && root.host);
            if (!next || next === scope) break;
            scope = next;
          }}
        }}
        anchors.push({{
          href,
          rawHref: current.getAttribute('href') || '',
          ownText: textOf(current),
          cardTitle: card
            ? textOf(card.querySelector('[slot="title"]'))
            : textOf(current),
          cardExtension: card ? String(card.getAttribute('extension') || '') : '',
          convertTo: current.getAttribute('data-convert_to') || ''
        }});
      }}
      if (current.shadowRoot) stack.push(current.shadowRoot);
      if (current.childNodes) stack.push(...current.childNodes);
    }}
    return {{anchors}};
  }});

  try {{
    const homeResponse = await page.goto(cfg.entryBaseUrl,
      {{waitUntil: 'domcontentloaded', timeout: 60000}});
    assertCurrentOrigin(entryOrigin);
    if (!homeResponse || !homeResponse.ok()) {{
      throw new Error(`固定来源首页不可用：HTTP ${{homeResponse ? homeResponse.status() : 'unknown'}}`);
    }}
    if (page.context().serviceWorkers().length) {{
      throw new Error('固定来源注册了 Service Worker，已停止');
    }}
    blockedNavigation = '';
    await page.goto(cfg.searchBaseUrl + 's/' + encodeURIComponent(cfg.title),
      {{waitUntil: 'domcontentloaded', timeout: 60000}});
    await page.waitForTimeout(4000);
    assertCurrentOrigin(searchOrigin);
    if (blockedNavigation) {{
      throw new Error(`固定来源搜索试图跳转到其他站点：${{blockedNavigation}}`);
    }}

    const listing = await deepData();
    const requested = normalize(cfg.title);
    const bookLinks = listing.anchors.filter(item => {{
      try {{
        return originOf(item.href) === searchOrigin
          && /\\/book\\/[^/]+\\/[^/]+/.test(pathnameOf(item.href));
      }} catch {{ return false; }}
    }});
    const titleMatches = bookLinks.filter(
      item => normalize(item.cardTitle) === requested);
    const books = titleMatches.filter(
      item => item.cardExtension.trim().toLowerCase() === 'pdf');
    if (!books.length) throw new Error(
      '站内页面未找到书名匹配的原始 PDF（链接 ' + bookLinks.length +
      '，书名匹配 ' + titleMatches.length + '，PDF ' + books.length +
      '）');
    await page.goto(books[0].href, {{waitUntil: 'domcontentloaded', timeout: 60000}});
    await page.waitForTimeout(3000);
    assertCurrentOrigin(searchOrigin);

    const detail = await deepData();
    const link = detail.anchors.find(item => {{
      try {{
        return originOf(item.href) === searchOrigin && pathnameOf(item.href).includes('/dl/')
          && /\\bPDF\\b/i.test(item.ownText)
          && item.convertTo.toLowerCase() !== 'pdf'
          && !isConvertedPdfUrl(item.href);
      }} catch {{ return false; }}
    }});
    if (!link) throw new Error('详情页没有原始 PDF 下载链接');
    const transfer = await page.context().request.get(link.href, {{
      maxRedirects: 0, timeout: 45000
    }});
    const transferStatus = transfer.status();
    let downloadUrl = link.href;
    if (transferStatus >= 300 && transferStatus < 400) {{
      const location = transfer.headers()['location'] || '';
      if (!location) throw new Error('PDF 下载入口返回重定向但缺少 Location');
      if (location.toLowerCase().startsWith('https://')) {{
        downloadUrl = location;
      }} else if (location.startsWith('/')) {{
        downloadUrl = searchOrigin + location;
      }} else {{
        throw new Error('PDF 下载入口返回了无法验证的相对 Location');
      }}
    }} else if (transferStatus < 200 || transferStatus >= 300) {{
      throw new Error('PDF 下载入口不可用：HTTP ' + transferStatus);
    }}
    return {{status: 'link', bookUrl: books[0].href,
      sourceDownloadUrl: link.href, downloadUrl, transferStatus}};
  }} catch (error) {{
    await page.screenshot({{path: cfg.debugPath, fullPage: true}}).catch(() => {{}});
    return {{status: 'error', error: String(error && error.message || error),
      currentUrl: page.url(), blockedNavigation}};
  }}
}}"""


def build_login_guard_code() -> str:
    """Open the confirmed search host while blocking every other site."""

    config = json.dumps(
        {
            "loginUrl": urllib.parse.urljoin(SEARCH_BASE_URL, "/login"),
            "allowedOrigins": sorted(ALLOWED_SITE_ORIGINS),
        },
        ensure_ascii=False,
    )
    return f"""async (page) => {{
  const cfg = {config};
  const allowedOrigins = new Set(cfg.allowedOrigins);
  let blockedNavigation = '';
  await page.context().addInitScript(() => {{
    try {{
      ServiceWorkerContainer.prototype.register = () =>
        Promise.reject(new Error('Service workers are disabled by Hermes'));
    }} catch {{}}
  }});
  const originOf = value => {{
    const match = String(value || '').match(/^(https?):\\/\\/([^/?#]+)/i);
    return match ? match[1].toLowerCase() + '://' + match[2].toLowerCase() : '';
  }};
  await page.context().route('**/*', async route => {{
    const request = route.request();
    const origin = originOf(request.url());
    if (!allowedOrigins.has(origin)) {{
      if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {{
        blockedNavigation = request.url();
      }}
      await route.abort('blockedbyclient');
      return;
    }}
    await route.continue();
  }});
  try {{
    const response = await page.goto(cfg.loginUrl,
      {{waitUntil: 'domcontentloaded', timeout: 60000}});
    const currentOrigin = originOf(page.url());
    const httpStatus = response ? response.status() : 0;
    if (!allowedOrigins.has(currentOrigin)) {{
      throw new Error(`登录页离开固定来源：${{currentOrigin}}`);
    }}
    if (page.context().serviceWorkers().length) {{
      throw new Error('登录页注册了 Service Worker，已停止');
    }}
    if (!response || !response.ok()) {{
      throw new Error(`登录页不可用：HTTP ${{httpStatus || 'unknown'}}`);
    }}
    return {{status: 'ready', currentUrl: page.url(), httpStatus}};
  }} catch (error) {{
    return {{status: 'error', error: String(error && error.message || error),
      currentUrl: page.url(), blockedNavigation}};
  }}
}}"""


def build_login_final_check_code() -> str:
    """Verify every browser page is still on the confirmed site chain."""

    config = json.dumps(
        {"allowedOrigins": sorted(ALLOWED_SITE_ORIGINS)}, ensure_ascii=False
    )
    return f"""async (page) => {{
  const cfg = {config};
  const allowedOrigins = new Set(cfg.allowedOrigins);
  const originOf = value => {{
    const match = String(value || '').match(/^(https?):\\/\\/([^/?#]+)/i);
    return match ? match[1].toLowerCase() + '://' + match[2].toLowerCase() : '';
  }};
  const pages = page.context().pages();
  const urls = pages.map(item => item.url());
  const workers = page.context().serviceWorkers();
  const invalid = pages.filter(item => !allowedOrigins.has(originOf(item.url())));
  for (const item of invalid) {{
    if (item !== page) await item.close().catch(() => {{}});
  }}
  for (const worker of workers) {{
    await worker.evaluate(() => self.registration.unregister()).catch(() => {{}});
  }}
  if (invalid.length || workers.length
      || !allowedOrigins.has(originOf(page.url()))) {{
    return {{status: 'error', error: '登录浏览器出现固定来源之外的页面',
      urls, serviceWorkers: workers.map(item => item.url())}};
  }}
  return {{status: 'ready', currentUrl: page.url(), pageCount: pages.length}};
}}"""


def build_site_probe_code() -> str:
    """Probe the confirmed entry page and the live .im search UI."""

    config = json.dumps(
        {
            "entryBaseUrl": ENTRY_BASE_URL,
            "searchAction": urllib.parse.urljoin(SEARCH_BASE_URL, "/s"),
            "allowedOrigins": sorted(ALLOWED_SITE_ORIGINS),
        },
        ensure_ascii=False,
    )
    return f"""async (page) => {{
  const cfg = {config};
  const allowedOrigins = new Set(cfg.allowedOrigins);
  let blockedNavigation = '';
  await page.context().addInitScript(() => {{
    try {{
      ServiceWorkerContainer.prototype.register = () =>
        Promise.reject(new Error('Service workers are disabled by Hermes'));
    }} catch {{}}
  }});
  const originOf = value => {{
    const match = String(value || '').match(/^(https?):\\/\\/([^/?#]+)/i);
    return match ? match[1].toLowerCase() + '://' + match[2].toLowerCase() : '';
  }};
  await page.context().route('**/*', async route => {{
    const request = route.request();
    const origin = originOf(request.url());
    if (!allowedOrigins.has(origin)) {{
      if (request.isNavigationRequest() && request.frame() === page.mainFrame()) {{
        blockedNavigation = request.url();
      }}
      await route.abort('blockedbyclient');
      return;
    }}
    await route.continue();
  }});

  let homepage = false;
  let homepageStatus = 0;
  let homepageFinalUrl = page.url();
  let homepageError = '';
  try {{
    const response = await page.goto(cfg.entryBaseUrl,
      {{waitUntil: 'domcontentloaded', timeout: 60000}});
    homepageStatus = response ? response.status() : 0;
    homepageFinalUrl = page.url();
    homepage = Boolean(response) && response.ok()
      && originOf(homepageFinalUrl) === originOf(cfg.entryBaseUrl)
      && page.context().serviceWorkers().length === 0;
  }} catch (error) {{
    homepageError = String(error && error.message || error);
    homepageFinalUrl = page.url();
  }}

  let searchBackend = false;
  let searchStatus = 0;
  let searchFinalUrl = '';
  let searchError = '';
  let searchUiValid = false;
  try {{
    await page.waitForTimeout(3000);
    const actions = await page.evaluate(() =>
      Array.from(document.querySelectorAll('form')).map(form => form.action));
    const normalizeAction = value => {{
      const text = String(value || '');
      return text.endsWith('/') ? text.slice(0, -1) : text;
    }};
    searchFinalUrl = actions.find(value =>
      normalizeAction(value) === cfg.searchAction) || '';
    searchUiValid = normalizeAction(searchFinalUrl) === cfg.searchAction;
    searchBackend = homepage && searchUiValid
      && originOf(searchFinalUrl) === originOf(cfg.searchAction)
      && !blockedNavigation && page.context().serviceWorkers().length === 0;
  }} catch (error) {{
    searchError = String(error && error.message || error);
  }}
  return {{homepage, homepageStatus, homepageFinalUrl, homepageError,
    searchBackend, searchStatus, searchUiValid,
    searchFinalUrl, searchError,
    blockedNavigation}};
}}"""


def parse_cli_json(stdout: str) -> dict[str, Any]:
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise SkillError("Playwright CLI 未返回有效 JSON。", code=3) from exc
    if not isinstance(envelope, dict):
        raise SkillError("Playwright CLI 返回结构异常。", code=3)
    if envelope.get("isError"):
        raise SkillError(f"Playwright 执行失败：{envelope.get('error')}", code=3)
    result = envelope.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as exc:
            raise SkillError("Playwright 结果无法解析。", code=3) from exc
    if not isinstance(result, dict):
        raise SkillError("Playwright 结果结构异常。", code=3)
    return result


def check_site_with_browser(
    cli: Sequence[str], storage_state: Path | None = None
) -> dict[str, Any]:
    validate_download_url(ENTRY_BASE_URL)
    validate_download_url(SEARCH_BASE_URL)
    session = f"zlibprobe-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        run_cli(cli, session, ["open", "about:blank"], timeout=60)
        if storage_state is not None:
            _, cookie_values = cookie_header(storage_state)
            if not has_authenticated_session(cookie_values):
                raise SkillError("登录状态缺少必要的 Z-Library 认证 Cookie。")
            run_cli(cli, session, ["state-load", str(storage_state)])
        stdout = run_cli(
            cli,
            session,
            ["run-code", build_site_probe_code()],
            json_output=True,
            timeout=120,
        )
        return parse_cli_json(stdout)
    finally:
        close_session(cli, session)


def browser_fallback(
    title: str,
    storage_state: Path | None,
    state_dir: Path,
) -> dict[str, Any]:
    validate_download_url(ENTRY_BASE_URL)
    validate_download_url(SEARCH_BASE_URL)
    cli = resolve_playwright_cli()
    session = f"zlibobs-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    debug_path = state_dir / "last-browser-error.png"
    debug_path.unlink(missing_ok=True)
    code_path = state_dir / f"browser-{uuid.uuid4().hex}.js"
    code_path.write_text(
        build_browser_fallback_code(title, debug_path), encoding="utf-8"
    )
    try:
        code_path.chmod(0o600)
    except OSError:
        pass
    try:
        run_cli(cli, session, ["open", "about:blank"])
        if storage_state is not None and storage_state.is_file():
            _, cookie_values = cookie_header(storage_state)
            if has_authenticated_session(cookie_values):
                run_cli(cli, session, ["state-load", str(storage_state)])
        stdout = run_cli(
            cli,
            session,
            ["run-code", "--filename", str(code_path)],
            json_output=True,
        )
        result = parse_cli_json(stdout)
        if result.get("status") != "link" or not result.get("downloadUrl"):
            raise SkillError(
                f"站内 UI 回退失败：{result.get('error', '未知错误')}",
                code=4,
                details=result,
            )
        return result
    finally:
        close_session(cli, session)
        code_path.unlink(missing_ok=True)
        if debug_path.is_file():
            try:
                debug_path.chmod(0o600)
            except OSError:
                pass


def perform_login(state_dir: Path) -> dict[str, Any]:
    validate_download_url(ENTRY_BASE_URL)
    validate_download_url(SEARCH_BASE_URL)
    cli = resolve_playwright_cli()
    storage_state = state_dir / STORAGE_STATE_NAME
    temporary_state = state_dir / f".{STORAGE_STATE_NAME}.{uuid.uuid4().hex}.tmp"
    session = f"zliblogin-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    try:
        run_cli(cli, session, ["open", "about:blank", "--headed"], timeout=60)
        guard_stdout = run_cli(
            cli,
            session,
            ["run-code", build_login_guard_code()],
            json_output=True,
            timeout=75,
        )
        guard_result = parse_cli_json(guard_stdout)
        if guard_result.get("status") != "ready":
            raise SkillError(
                f"固定来源登录页无法安全打开：{guard_result.get('error', '未知错误')}",
                code=3,
                details=guard_result,
            )
        print("请在打开的浏览器中完成 Z-Library 登录。", flush=True)
        try:
            input("登录完成后回到终端，按 Enter 保存会话：")
        except EOFError as exc:
            raise SkillError("登录命令必须在本地交互式终端运行。") from exc
        final_stdout = run_cli(
            cli,
            session,
            ["run-code", build_login_final_check_code()],
            json_output=True,
            timeout=30,
        )
        final_result = parse_cli_json(final_stdout)
        if final_result.get("status") != "ready":
            raise SkillError(
                f"登录状态保存前检查失败：{final_result.get('error', '未知错误')}",
                details=final_result,
            )
        run_cli(cli, session, ["state-save", str(temporary_state)])
        if not temporary_state.is_file():
            raise SkillError("Playwright 未生成登录状态文件。")
        temporary_state.chmod(0o600)
        _, cookie_values = cookie_header(temporary_state)
        if not has_authenticated_session(cookie_values):
            raise SkillError(
                "登录状态缺少 remix_userid/remix_userkey；请确认登录完成。"
            )
        os.replace(temporary_state, storage_state)
        storage_state.chmod(0o600)
        return {"status": "logged-in", "storage_state": str(storage_state)}
    finally:
        close_session(cli, session)
        temporary_state.unlink(missing_ok=True)


def check_dns() -> tuple[bool, str | None]:
    try:
        validate_download_url(ENTRY_BASE_URL)
        validate_download_url(SEARCH_BASE_URL)
        return True, None
    except SkillError as exc:
        return False, str(exc)


def check_http_search_backend(storage_state: Path) -> tuple[bool, str | None]:
    try:
        cookie, cookie_values = cookie_header(storage_state)
        if not has_authenticated_session(cookie_values):
            raise SkillError("登录状态缺少必要的 Z-Library 认证 Cookie。")
        body, boundary = multipart_body(
            {
                "q": "__hermes_healthcheck__",
                "page": "1",
                "limit": "1",
                "order": "popular",
            }
        )
        payload = request_json(
            urllib.parse.urljoin(BASE_URL, "/api/search"),
            cookie=cookie,
            data=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )
        if not has_books_schema(payload):
            raise SkillError("站内 API JSON 缺少 books 列表结构。", code=3)
        return True, None
    except SkillError as exc:
        return False, str(exc)


def doctor(vault_arg: str | None, state_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "site": ENTRY_BASE_URL,
        "search_site": SEARCH_BASE_URL,
    }
    result["dns"], result["dns_error"] = check_dns()
    cli: list[str] | None = None
    storage_state = state_dir / STORAGE_STATE_NAME
    if storage_state.is_file():
        try:
            _, cookie_values = cookie_header(storage_state)
            result["session"] = has_authenticated_session(cookie_values)
            if not result["session"]:
                result["session_error"] = (
                    "登录状态缺少 remix_userid/remix_userkey。"
                )
        except SkillError as exc:
            result["session"] = False
            result["session_error"] = str(exc)
    else:
        result["session"] = False
        result["session_error"] = "登录状态文件不存在。"
    result["session_path"] = str(storage_state)
    try:
        cli = resolve_playwright_cli()
        completed = subprocess.run(
            [*cli, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        result["playwright_cli"] = completed.returncode == 0
        result["playwright_version"] = completed.stdout.strip() or completed.stderr.strip()
    except (SkillError, subprocess.TimeoutExpired) as exc:
        result["playwright_cli"] = False
        result["playwright_error"] = str(exc)

    if result.get("playwright_cli") and cli and result["dns"]:
        try:
            probe = check_site_with_browser(
                cli, storage_state if result["session"] else None
            )
            result.update(
                {
                    "homepage": bool(probe.get("homepage")),
                    "homepage_http_status": probe.get("homepageStatus"),
                    "homepage_final_url": probe.get("homepageFinalUrl"),
                    "homepage_error": probe.get("homepageError") or None,
                    "search_backend": bool(probe.get("searchBackend")),
                    "search_http_status": probe.get("searchStatus"),
                    "search_ui_valid": bool(probe.get("searchUiValid")),
                    "search_final_url": probe.get("searchFinalUrl"),
                    "search_error": probe.get("searchError") or None,
                    "blocked_navigation": probe.get("blockedNavigation") or None,
                }
            )
        except SkillError as exc:
            result["homepage"] = False
            result["search_backend"] = False
            result["site_probe_error"] = str(exc)
    else:
        result["homepage"] = False
        result["search_backend"] = False

    try:
        result["vault_path"] = str(resolve_vault(vault_arg))
        result["vault"] = True
    except SkillError as exc:
        result["vault"] = False
        result["vault_error"] = str(exc)

    result["ready"] = all(
        bool(result.get(key))
        for key in (
            "playwright_cli",
            "homepage",
            "search_backend",
            "vault",
            "dns",
        )
    )
    return result


def download_book(title: str, vault: Path, state_dir: Path) -> dict[str, Any]:
    title = title.strip()
    if not title:
        raise SkillError("书名不能为空。")
    storage_state = state_dir / STORAGE_STATE_NAME

    temp_dir = state_dir / "downloads"
    temp_dir.mkdir(parents=True, exist_ok=True)
    try:
        temp_dir.chmod(0o700)
    except OSError:
        pass
    temp_path = temp_dir / f"{uuid.uuid4().hex}.download"
    try:
        try:
            browser_result = browser_fallback(
                title,
                storage_state if storage_state.is_file() else None,
                state_dir,
            )
            cookie = ""
            if storage_state.is_file():
                candidate_cookie, cookie_values = cookie_header(storage_state)
                if has_authenticated_session(cookie_values):
                    cookie = candidate_cookie
            http_download(
                str(browser_result["downloadUrl"]),
                temp_path,
                cookie=cookie,
                source_url=(
                    str(browser_result["sourceDownloadUrl"])
                    if browser_result.get("sourceDownloadUrl")
                    else None
                ),
            )
        except SkillError as exc:
            temp_path.unlink(missing_ok=True)
            raise SkillError(
                "受控站内页面流程未取得原始 PDF。",
                code=4,
                details={"attempts": [str(exc)]},
            ) from exc

        if not temp_path.is_file():
            raise SkillError(
                "受控站内页面流程未取得原始 PDF。",
                code=4,
            )
        result = commit_pdf(temp_path, vault, title)
        result.update(
            {"title": title, "site": ENTRY_BASE_URL, "search_site": SEARCH_BASE_URL}
        )
        return result
    finally:
        temp_path.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按书名从固定 Z-Library 站点保存原始 PDF 到 Obsidian。"
    )
    parser.add_argument("--state-dir", help="覆盖 Hermes 状态目录（主要用于测试）")
    parser.add_argument("--vault", help="覆盖 OBSIDIAN_VAULT_PATH（主要用于测试）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor_parser = subparsers.add_parser("doctor", help="检查依赖、Vault、会话和 DNS")
    doctor_parser.add_argument("--json", action="store_true", dest="as_json")

    subparsers.add_parser("login", help="一次性登录并保存浏览器状态")

    download_parser = subparsers.add_parser("download", help="按书名搜索并保存 PDF")
    download_parser.add_argument("title", help="完整书名")
    download_parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def emit(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    else:
        for key, value in payload.items():
            if value is not None:
                print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        state_dir = resolve_state_dir(args.state_dir)
        if args.command == "doctor":
            result = doctor(args.vault, state_dir)
            emit(result, as_json=args.as_json)
            return 0 if result["ready"] else 1
        if args.command == "login":
            result = perform_login(state_dir)
            emit(result, as_json=False)
            return 0
        if args.command == "download":
            vault = resolve_vault(args.vault)
            preflight = doctor(str(vault), state_dir)
            if not preflight.get("ready"):
                raise SkillError(
                    "运行前检查未通过；未开始书名搜索或 PDF 下载。",
                    code=3,
                    details=preflight,
                )
            result = download_book(args.title, vault, state_dir)
            emit(result, as_json=args.as_json)
            return 0 if result.get("status") in {"saved", "duplicate"} else 5
    except SkillError as exc:
        payload = {"status": "error", "error": str(exc)}
        if exc.details is not None:
            payload["details"] = exc.details
        emit(payload, as_json=getattr(args, "as_json", False))
        return exc.code
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
