#!/usr/bin/env python3
"""
Upload a markdown report to IMA as a new note.

Pure stdlib (urllib, json, os, sys). No third-party deps so this runs in any
Python 3.8+ GitHub-hosted runner without `pip install`.

Credentials come from environment variables — set these as repository secrets
under Settings → Secrets and variables → Actions:
    IMA_CLIENT_ID   — 来自 ima.qq.com 控制台
    IMA_API_KEY     — 来自 ima.qq.com 控制台

Usage:
    python3 ima_upload.py <markdown_file> [--title "..."] [--folder-id ID]

Exit codes:
    0  success
    1  bad arguments / missing credentials
    2  file not found / not UTF-8
    3  HTTP error
    4  API returned non-zero code
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# 文档: https://ima.qq.com (openapi)
API_BASE = "https://ima.qq.com"
IMPORT_DOC_PATH = "/openapi/note/v1/import_doc"
HTTP_TIMEOUT = 30  # seconds

# API 要求 content_format=1 (Markdown). title 是可选的, 缺省时 IMA 会用正文第一行作为标题.
CONTENT_FORMAT_MARKDOWN = 1


def _err(code: int, msg: str) -> "sys.exit":
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _read_credentials() -> tuple[str, str]:
    # 兼容两种命名 (IMA_OPENAPI_* 是 ima_api.cjs 用的; IMA_CLIENT_ID 更短, 优先用这个)
    client_id = (
        os.environ.get("IMA_CLIENT_ID")
        or os.environ.get("IMA_OPENAPI_CLIENTID")
        or ""
    ).strip()
    api_key = (
        os.environ.get("IMA_API_KEY")
        or os.environ.get("IMA_OPENAPI_APIKEY")
        or ""
    ).strip()
    if not client_id or not api_key:
        _err(
            1,
            "missing IMA credentials. Set IMA_CLIENT_ID and IMA_API_KEY env vars "
            "(e.g. via `gh secret set IMA_CLIENT_ID` or repo Settings → Secrets).",
        )
    return client_id, api_key


def _read_markdown(path: Path) -> str:
    # 二进制读取, 然后显式 UTF-8 解码. 任何解码失败都按 exit 2 处理
    # (API 不接受坏 UTF-8, 会造成笔记里出现乱码)
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        _err(2, f"{path} is not valid UTF-8: {e}")
    # 二次校验, 防止有 surrogate 等异常情况被默收
    text.encode("utf-8").decode("utf-8")
    return text


def _post_import_doc(client_id: str, api_key: str,
                     content: str, title: str | None,
                     folder_id: str | None) -> dict:
    body: dict = {"content_format": CONTENT_FORMAT_MARKDOWN, "content": content}
    if title:
        body["title"] = title
    if folder_id:
        body["folder_id"] = folder_id

    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}{IMPORT_DOC_PATH}",
        data=payload,
        method="POST",
        headers={
            "ima-openapi-clientid": client_id,
            "ima-openapi-apikey": api_key,
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "cn-stock-analyzer/1.0 (github-actions)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as e:
        # 服务端返回的 4xx/5xx 也算 HTTP 错误, 但 body 里可能含具体错误码
        body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        _err(3, f"HTTP {e.code} {e.reason}: {body_text[:500]}")
    except urllib.error.URLError as e:
        _err(3, f"network error: {e.reason}")

    if status != 200:
        _err(3, f"unexpected HTTP status {status}: {raw[:500]}")

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        _err(3, f"non-JSON response ({e}): {raw[:500]}")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload a markdown file as a new IMA note.",
    )
    parser.add_argument("file", type=Path, help="path to markdown file to upload")
    parser.add_argument("--title", help="note title (default: file's first H1)")
    parser.add_argument("--folder-id", help="target folder id (optional)")
    args = parser.parse_args()

    if not args.file.exists():
        _err(2, f"file not found: {args.file}")

    client_id, api_key = _read_credentials()
    content = _read_markdown(args.file)

    title = args.title
    if not title:
        # 从 markdown 第一行 (# ...) 提取作为默认标题, 避免无标题笔记
        for line in content.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith("# "):
                title = stripped[2:].strip()[:120]
                break
            # 没有 H1, 用文件名代替
            title = args.file.stem[:120]
            break
        else:
            title = args.file.stem[:120]

    print(f"Uploading {args.file} ({len(content)} bytes) as note: {title!r}")
    resp = _post_import_doc(client_id, api_key, content, title, args.folder_id)

    if resp.get("code") not in (0, "0"):
        _err(4, f"API error: code={resp.get('code')} msg={resp.get('msg')} raw={resp}")

    note_id = (resp.get("data") or {}).get("note_id", "<unknown>")
    print(f"OK note_id={note_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
