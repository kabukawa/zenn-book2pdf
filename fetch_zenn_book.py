#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Zenn book -> Markdown + 印刷用 HTML + PDF

任意の Zenn の本の URL を渡すと、章ごとの Markdown、結合 HTML、PDF を生成します。

Usage:
    pip install -r requirements.txt
    playwright install chromium
    winget install --id calibre.calibre -e   # EPUB を作る場合
    zenn-book2pdf https://zenn.dev/USER/books/BOOK-SLUG
    zenn-book2pdf --epub URL
    zenn-book2pdf --pdf-only
    zenn-book2pdf --epub-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

VERSION = "1.0"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ZennBookFetcher/" + VERSION + ")"}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT_DIR = "output"
OUT_DIR = DEFAULT_OUT_DIR
IMG_DIR = os.path.join(OUT_DIR, "images")
CHAPTERS_DIR = os.path.join(OUT_DIR, "chapters")
CACHE_DIR = os.path.join(SCRIPT_DIR, "cache", "mermaid")

ZENN_ORIGIN = "https://zenn.dev"
DEFAULT_WORKERS = 6

DEFAULT_URL = (
    "https://zenn.dev/natsuking/books/credit-card-agentic-payments"
    "/viewer/chapter-01-why-start-from-payment-rails"
)

NOTE_DIAGRAM_FAIL = "【図: 図解（元記事を参照）】"
NOTE_EMBED_FAIL = "【埋め込みコンテンツ（元記事を参照）】"

MM_PER_PX = 25.4 / 96.0
MAX_DIAGRAM_WIDTH_MM = 170.0

MERMAID_JS_CACHE_PATH = os.path.join(SCRIPT_DIR, "mermaid.min.js")
MERMAID_JS_CDN_URL = "https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"

PDF_TIMEOUT_MS = 180000
PDF_ESTIMATE_PATH = os.path.join(SCRIPT_DIR, "cache", "pdf_last_sec.txt")
_BAR_WIDTH = 28

_VERBOSE = False
_LOG_LOCK = threading.Lock()
_status_active = False
_browser_state = {"pw": None, "browser": None, "mermaid_page": None}

_MERMAID_INK_LOCK = threading.Lock()
_MERMAID_MIN_INTERVAL = 2.0
_mermaid_last_request_at = [0.0]


def configure_paths(output_dir):
    global OUT_DIR, IMG_DIR, CHAPTERS_DIR
    OUT_DIR = output_dir
    IMG_DIR = os.path.join(OUT_DIR, "images")
    CHAPTERS_DIR = os.path.join(OUT_DIR, "chapters")


_WIN_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def book_output_stem(title, slug):
    """本のタイトルから、HTML/PDF 用のファイル名（拡張子なし）を作る。"""
    raw = (title or "").strip() or (slug or "book")
    raw = re.sub(r"[\r\n\t]+", " ", raw)
    raw = raw.replace("\u00a0", " ")
    raw = re.sub(r'[<>:"/\\|?*]', "", raw)
    raw = "".join(ch for ch in raw if ord(ch) >= 32)
    raw = re.sub(r"\s+", " ", raw).strip(" .")
    if not raw or raw.upper() in _WIN_RESERVED_NAMES:
        raw = re.sub(r'[<>:"/\\|?*]', "-", (slug or "book")).strip(" .") or "book"
    if len(raw) > 120:
        raw = raw[:120].rstrip(" .")
    return raw


_CALIBRE_TEMP_NAMES = {
    "zenn_in.html",
    "zenn_calibre_in.html",
    "_zenn_calibre_in.html",
}


def list_output_htmls(out_dir):
    if not os.path.isdir(out_dir):
        return []
    found = []
    for name in os.listdir(out_dir):
        if not name.lower().endswith(".html"):
            continue
        if name.lower() in _CALIBRE_TEMP_NAMES:
            continue
        path = os.path.join(out_dir, name)
        if os.path.isfile(path):
            found.append(path)
    found.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return found


def find_book_html_for_pdf(out_dir):
    htmls = list_output_htmls(out_dir)
    if not htmls:
        return None
    return htmls[0]


def clear_previous_book_files(out_dir):
    """前回の結合 HTML/PDF を消す（images / chapters 以外）。"""
    if not os.path.isdir(out_dir):
        return
    for name in os.listdir(out_dir):
        lower = name.lower()
        if lower.endswith((".html", ".pdf", ".epub", ".mobi", ".azw3")):
            try:
                os.remove(os.path.join(out_dir, name))
            except Exception:
                pass


def log(msg=""):
    global _status_active
    with _LOG_LOCK:
        if _status_active:
            sys.stdout.write("\n")
            _status_active = False
        print(msg, flush=True)


def log_verbose(msg):
    if _VERBOSE:
        log(msg)


def format_elapsed(seconds):
    seconds = int(max(0, seconds))
    minutes, sec = divmod(seconds, 60)
    if minutes:
        return "%d分%d秒" % (minutes, sec)
    return "%d秒" % sec


def _stdout_is_tty():
    try:
        return bool(sys.stdout.isatty())
    except Exception:
        return False


def _write_status(msg):
    """TTY なら同じ行を上書き、パイプなら通常の行出力。"""
    global _status_active
    with _LOG_LOCK:
        if _stdout_is_tty():
            sys.stdout.write("\r" + msg + "\x1b[K")
            sys.stdout.flush()
            _status_active = True
        else:
            print(msg, flush=True)
            _status_active = False


def _end_status(msg=None):
    global _status_active
    with _LOG_LOCK:
        if _status_active:
            if msg:
                sys.stdout.write("\r" + msg + "\x1b[K\n")
            else:
                sys.stdout.write("\x1b[K\n")
            sys.stdout.flush()
            _status_active = False
        elif msg:
            print(msg, flush=True)


def render_bar(ratio, width=_BAR_WIDTH):
    ratio = max(0.0, min(1.0, float(ratio)))
    filled = int(round(width * ratio))
    if filled > width:
        filled = width
    return "#" * filled + "-" * (width - filled)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Zennの本を取得し、印刷用HTML・PDF・EPUBを生成します。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例:\n"
            "  zenn-book2pdf https://zenn.dev/USER/books/BOOK-SLUG\n"
            "  zenn-book2pdf --epub URL\n"
            "  zenn-book2pdf --pdf-only\n"
            "  zenn-book2pdf --epub-only\n"
            "  zenn-book2pdf --html-only URL\n"
            "  .\\zenn-book2pdf.ps1 --epub URL\n"
            "  ./zenn-book2pdf.sh --epub URL\n"
        ),
    )
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version="zenn-book2pdf v" + VERSION,
        help="バージョンを表示して終了する",
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=None,
        help="本の章ビューアまたはトップページの URL（省略時は内蔵のデフォルト）",
    )
    parser.add_argument(
        "--html-only",
        action="store_true",
        help="PDF を生成せず、Markdown と HTML までで止める",
    )
    parser.add_argument(
        "--pdf-only",
        action="store_true",
        help="既存の結合 HTML から PDF だけ生成する（再取得しない）",
    )
    parser.add_argument(
        "--epub",
        action="store_true",
        help="EPUB も生成する（Calibre の ebook-convert が必要。Kindle 向け）",
    )
    parser.add_argument(
        "--epub-only",
        action="store_true",
        help="既存の結合 HTML から EPUB だけ生成する（再取得しない）",
    )
    parser.add_argument(
        "--no-epub",
        action="store_true",
        help="EPUB を生成しない（--epub を打ち消す）",
    )
    parser.add_argument(
        "--output-dir",
        default=DEFAULT_OUT_DIR,
        help="出力フォルダ（既定: output）",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="章取得の並列数（既定: %d）" % DEFAULT_WORKERS,
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Playwright の例外など詳細ログを出す",
    )
    parser.add_argument(
        "--no-mermaid-cache",
        action="store_true",
        help="図のディスクキャッシュを使わず全部描き直す",
    )
    args = parser.parse_args(argv)
    if args.html_only and args.pdf_only:
        parser.error("--html-only と --pdf-only は同時に指定できません")
    if args.html_only and args.epub_only:
        parser.error("--html-only と --epub-only は同時に指定できません")
    if args.no_epub and args.epub_only:
        parser.error("--no-epub と --epub-only は同時に指定できません")
    if args.no_epub and args.epub:
        parser.error("--no-epub と --epub は同時に指定できません")
    if args.workers < 1:
        parser.error("--workers は 1 以上にしてください")
    return args


# ---------------------------------------------------------------------------
# 本と章の取得
# ---------------------------------------------------------------------------

def parse_book_url(url):
    """Extract (username, book_slug) from any Zenn book/viewer URL."""
    m = re.search(r"zenn\.dev/([^/]+)/books/([^/?#]+)", url)
    if not m:
        raise ValueError("Zenn の本の URL として解釈できません: " + url)
    return m.group(1), m.group(2)


def fetch_page_title(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        if soup.title:
            return soup.title.get_text().split("|")[0].strip()
    except Exception:
        pass
    return None


def extract_book_meta(data, username):
    """Best-effort extraction of title / subtitle / author from the book API response."""
    book_node = data
    if isinstance(data, dict) and isinstance(data.get("book"), dict):
        book_node = data["book"]

    title = None
    subtitle = None
    author = None

    if isinstance(book_node, dict):
        title = book_node.get("title")
        subtitle = book_node.get("summary") or book_node.get("description")
        user_node = book_node.get("user")
        if isinstance(user_node, dict):
            author = user_node.get("name") or user_node.get("username")

    if not author:
        author = username

    return title, subtitle, author


def extract_cover_image_url(data, username, book_slug):
    """表紙 URL。API を試し、無ければ本のトップページの og:image を使う。"""
    book_node = None
    if isinstance(data, dict):
        book_node = data.get("book") if isinstance(data.get("book"), dict) else data

    if isinstance(book_node, dict):
        for key in ("cover_image_url", "coverImageUrl", "image_url", "imageUrl", "image"):
            val = book_node.get(key)
            if isinstance(val, str) and val.startswith("http"):
                return val

    try:
        book_page_url = ZENN_ORIGIN + "/" + username + "/books/" + book_slug
        r = requests.get(book_page_url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        og = soup.find("meta", attrs={"property": "og:image"})
        if og and isinstance(og.get("content"), str) and og["content"].startswith("http"):
            return og["content"]
    except Exception:
        pass

    return None


def find_explicit_chapters_list(data, book_slug):
    """Look for the book API's own authoritative chapter list."""
    candidate_lists = []
    if isinstance(data, dict):
        if isinstance(data.get("chapters"), list):
            candidate_lists.append(data["chapters"])
        book_node = data.get("book")
        if isinstance(book_node, dict) and isinstance(book_node.get("chapters"), list):
            candidate_lists.append(book_node["chapters"])

    for lst in candidate_lists:
        chapters = [
            c for c in lst
            if isinstance(c, dict) and c.get("slug") and c.get("slug") != book_slug
        ]
        if chapters:
            chapters.sort(key=lambda c: c.get("position", 0))
            return chapters
    return []


def find_chapters_via_generic_walk(data, book_slug):
    """Fallback only: strict recursive scan requiring id + position + title."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if (
                node.get("slug")
                and node.get("slug") != book_slug
                and "position" in node
                and "id" in node
                and "title" in node
            ):
                found.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)

    by_slug = {}
    for item in found:
        slug = item.get("slug")
        if slug and (slug not in by_slug or len(item) > len(by_slug[slug])):
            by_slug[slug] = item

    chapters = list(by_slug.values())
    chapters.sort(key=lambda c: c.get("position", 0))
    return chapters


def find_chapters_via_api(book_slug):
    """Try Zenn's book API to get an ordered chapter list plus raw response."""
    api_url = ZENN_ORIGIN + "/api/books/" + book_slug
    r = requests.get(api_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    data = r.json()

    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "_debug_book_api.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    chapters = find_explicit_chapters_list(data, book_slug)
    if not chapters:
        log("  章一覧が API に無かったので、応答を走査します")
        chapters = find_chapters_via_generic_walk(data, book_slug)

    return data, chapters


def find_chapters_via_scrape(start_url, username, book_slug):
    """Last-resort fallback: scrape chapter viewer links from the page HTML."""
    r = requests.get(start_url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    html = r.text

    link_prefix = "/" + username + "/books/" + book_slug + "/viewer/"
    pattern = re.compile(re.escape(link_prefix) + r"([a-zA-Z0-9\-_]+)")
    seen = []
    for m in pattern.finditer(html):
        slug = m.group(1)
        if slug not in seen:
            seen.append(slug)

    chapters = [{"slug": s, "position": i + 1} for i, s in enumerate(seen)]

    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text().split("|")[0].strip() if soup.title else None

    return title, chapters


def fetch_chapter_by_id(cid):
    url = ZENN_ORIGIN + "/api/chapters/" + str(cid)
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()["chapter"]


def fetch_chapter_by_slug(username, book_slug, chapter_slug):
    """Fallback when we only know the slug (no numeric id from the API)."""
    url = ZENN_ORIGIN + "/" + username + "/books/" + book_slug + "/viewer/" + chapter_slug
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else chapter_slug

    content = soup.select_one(".znc") or soup.select_one("article") or soup.find("body")
    body_html = str(content) if content else ""

    return {"title": title, "body_html": body_html}


# ---------------------------------------------------------------------------
# 画像ダウンロード
# ---------------------------------------------------------------------------

def download_binary(url, out_dir, filename):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def download_image(url):
    ext = os.path.splitext(url.split("?")[0])[1]
    if not ext or len(ext) > 5:
        ext = ".png"
    fname = "img_" + uuid.uuid4().hex[:12] + ext
    try:
        download_binary(url, IMG_DIR, fname)
        return "images/" + fname
    except Exception as e:
        log("  ! 画像のダウンロードに失敗: " + url + " (" + str(e) + ")")
        return url


# ---------------------------------------------------------------------------
# Mermaid SVG の後処理（サイズ・XML として妥当な void タグ）
# ---------------------------------------------------------------------------

_VOID_HTML_TAGS = (
    "br", "hr", "img", "input", "meta", "link",
    "area", "base", "col", "embed", "source", "track", "wbr",
)


def _fix_html_void_tags_for_xml(svg_text):
    """<br> など HTML の void タグを SVG として妥当な自己閉じに直す。"""
    def _self_close(m):
        tag = m.group(0)
        if tag.rstrip().endswith("/>"):
            return tag
        return tag[:-1].rstrip() + "/>"

    pattern = r"<(?:" + "|".join(_VOID_HTML_TAGS) + r")\b[^>]*>"
    return re.sub(pattern, _self_close, svg_text, flags=re.IGNORECASE)


def _bake_svg_physical_size(svg_text, width_mm, height_mm):
    """<svg> の width/height を mm にして、Markdown 経由でも実寸が残るようにする。"""
    def _replace_root_tag(m):
        tag = m.group(0)
        tag = re.sub(r'\swidth="[^"]*"', "", tag, count=1)
        tag = re.sub(r'\sheight="[^"]*"', "", tag, count=1)
        tag = re.sub(r'\sstyle="[^"]*"', "", tag, count=1)
        return tag.replace(
            "<svg",
            '<svg width="' + ("%.2f" % width_mm) + 'mm" height="' + ("%.2f" % height_mm) + 'mm"',
            1,
        )

    return re.sub(r"<svg\b[^>]*>", _replace_root_tag, svg_text, count=1)


def _compute_svg_physical_size(svg_text):
    """viewBox から (width_mm, height_mm) を求める。無ければ (None, None)。"""
    m = re.search(r'viewBox="[\-\d.]+\s+[\-\d.]+\s+([\d.]+)\s+([\d.]+)"', svg_text)
    if not m:
        return None, None
    vb_width = float(m.group(1))
    vb_height = float(m.group(2))
    if vb_width <= 0:
        return None, None
    width_mm = min(vb_width * MM_PER_PX, MAX_DIAGRAM_WIDTH_MM)
    scale = width_mm / (vb_width * MM_PER_PX)
    height_mm = vb_height * MM_PER_PX * scale
    return width_mm, height_mm


def _strip_mermaid_directives(mermaid_src):
    """%%{init: ...}%% を除き、図ごとの fontSize 上書きで大きさがばらつくのを防ぐ。"""
    return re.sub(r"%%\{.*?\}%%", "", mermaid_src, flags=re.DOTALL)


def mermaid_hash(mermaid_src):
    normalized = _strip_mermaid_directives(mermaid_src).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _save_svg_with_size(svg_text, dest_path):
    svg_text = _fix_html_void_tags_for_xml(svg_text)
    width_mm, height_mm = _compute_svg_physical_size(svg_text)
    if width_mm:
        svg_text = _bake_svg_physical_size(svg_text, width_mm, height_mm)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write(svg_text)
    return width_mm


def _mermaid_throttled_get(url):
    with _MERMAID_INK_LOCK:
        wait = _mermaid_last_request_at[0] + _MERMAID_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        try:
            return requests.get(url, headers=HEADERS, timeout=30)
        finally:
            _mermaid_last_request_at[0] = time.monotonic()


def _render_mermaid_via_service(mermaid_src):
    """mermaid.ink への最後の手段。SVG を試し、だめなら PNG。"""
    import base64

    mermaid_src = _strip_mermaid_directives(mermaid_src)
    encoded = base64.urlsafe_b64encode(mermaid_src.encode("utf-8")).decode("ascii")
    retries = 2

    svg_url = "https://mermaid.ink/svg/" + encoded
    last_err = None
    for attempt in range(retries):
        try:
            r = _mermaid_throttled_get(svg_url)
            r.raise_for_status()
            fname = "mermaid_" + mermaid_hash(mermaid_src) + ".svg"
            dest = os.path.join(IMG_DIR, fname)
            width_mm = _save_svg_with_size(r.text, dest)
            return "images/" + fname, width_mm
        except Exception as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
    log("  ! mermaid.ink の SVG が失敗したので PNG を試します (" + _short_err(last_err) + ")")

    img_url = "https://mermaid.ink/img/" + encoded + "?type=png"
    last_err = None
    for attempt in range(retries):
        try:
            r = _mermaid_throttled_get(img_url)
            r.raise_for_status()
            png_bytes = r.content
            fname_png = "mermaid_" + mermaid_hash(mermaid_src) + ".png"
            os.makedirs(IMG_DIR, exist_ok=True)
            with open(os.path.join(IMG_DIR, fname_png), "wb") as f:
                f.write(png_bytes)
            width_mm = None
            if len(png_bytes) >= 24 and png_bytes[12:16] == b"IHDR":
                px_width = int.from_bytes(png_bytes[16:20], "big")
                width_mm = min(px_width * MM_PER_PX, MAX_DIAGRAM_WIDTH_MM)
            return "images/" + fname_png, width_mm
        except Exception as e2:
            last_err = e2
            if attempt < retries - 1:
                time.sleep(4 * (attempt + 1))
    log("  ! mermaid.ink の PNG も失敗しました: " + _short_err(last_err))
    return None, None


def _ensure_mermaid_js():
    if os.path.exists(MERMAID_JS_CACHE_PATH):
        return MERMAID_JS_CACHE_PATH
    try:
        log("  mermaid.js をダウンロードしています...")
        r = requests.get(MERMAID_JS_CDN_URL, headers=HEADERS, timeout=30)
        r.raise_for_status()
        with open(MERMAID_JS_CACHE_PATH, "w", encoding="utf-8") as f:
            f.write(r.text)
        return MERMAID_JS_CACHE_PATH
    except Exception as e:
        log("  ! mermaid.js をダウンロードできませんでした (" + str(e) + ")")
        return None


def launch_browser():
    """Playwright の Chromium を起動する。失敗時は (None, 日本語メッセージ)。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, (
            "Playwright がインストールされていません。次を実行してください:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )

    pw = sync_playwright().start()
    last_err = None
    browser = None
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            browser = pw.chromium.launch(**kwargs)
            break
        except Exception as e:
            last_err = e
            browser = None

    if browser is None:
        try:
            pw.stop()
        except Exception:
            pass
        return None, (
            "Chromium を起動できませんでした。次を実行してください:\n"
            "  playwright install chromium\n"
            "詳細: " + str(last_err)
        )

    _browser_state["pw"] = pw
    _browser_state["browser"] = browser
    return browser, None


def close_browser():
    page = _browser_state.get("mermaid_page")
    if page is not None:
        try:
            page.close()
        except Exception:
            pass
        _browser_state["mermaid_page"] = None
    browser = _browser_state.get("browser")
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
        _browser_state["browser"] = None
    pw = _browser_state.get("pw")
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass
        _browser_state["pw"] = None


def _get_mermaid_page(browser):
    page = _browser_state.get("mermaid_page")
    if page is not None:
        return page

    js_path = _ensure_mermaid_js()
    if not js_path:
        return None

    page = browser.new_page(
        viewport={"width": 2400, "height": 2400},
        device_scale_factor=1,
    )
    page.set_content("<!DOCTYPE html><html><head><meta charset='utf-8'></head><body></body></html>")
    page.add_script_tag(path=os.path.abspath(js_path))
    page.wait_for_function("() => typeof mermaid !== 'undefined'", timeout=20000)
    page.evaluate(
        """() => {
            mermaid.initialize({
                startOnLoad: false,
                theme: 'default',
                securityLevel: 'loose',
                themeVariables: { fontSize: '16px', background: 'transparent' }
            });
        }"""
    )
    _browser_state["mermaid_page"] = page
    return page


def _short_err(err):
    text = str(err)
    if "url:" in text:
        text = text.split("url:")[0].strip().rstrip("(").strip()
    if len(text) > 180:
        text = text[:180] + "..."
    return text


def _render_one_local(page, diagram_id, mermaid_src):
    # page.evaluate に timeout 引数は無い（渡すと TypeError になり mermaid.ink へ落ちる）
    result = page.evaluate(
        """async ({ id, src }) => {
            try {
                const out = await mermaid.render(id, src);
                document.body.innerHTML = '';
                return { ok: true, svg: out.svg };
            } catch (e) {
                document.body.innerHTML = '';
                const msg = (e && e.message) ? e.message : String(e);
                return { ok: false, error: msg };
            }
        }""",
        {"id": diagram_id, "src": mermaid_src},
    )
    if not result or not result.get("ok") or not result.get("svg"):
        raise RuntimeError((result or {}).get("error") or "mermaid.render が空の結果を返しました")
    return result["svg"]


def _render_one_local_via_pre(browser, mermaid_src):
    """mermaid.render が弾く図を、一時 HTML + startOnLoad で描く（旧経路）。"""
    js_path = _ensure_mermaid_js()
    if not js_path:
        raise RuntimeError("mermaid.js がありません")
    escaped = (
        mermaid_src.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    mermaid_js_url = "file:///" + os.path.abspath(js_path).replace("\\", "/")
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<script src='" + mermaid_js_url + "'></script></head>"
        "<body style='margin:0;background:#ffffff;'>"
        "<pre class='mermaid'>" + escaped + "</pre>"
        "<script>mermaid.initialize({startOnLoad:true, theme:'default', "
        "securityLevel:'loose', themeVariables:{fontSize:'16px'}});</script>"
        "</body></html>"
    )
    tmp_html_path = os.path.join(SCRIPT_DIR, "_mermaid_render_tmp.html")
    with open(tmp_html_path, "w", encoding="utf-8") as f:
        f.write(html)
    tmp_html_url = "file:///" + os.path.abspath(tmp_html_path).replace("\\", "/")
    page = browser.new_page(
        viewport={"width": 2400, "height": 2400},
        device_scale_factor=1,
    )
    try:
        page.goto(tmp_html_url, timeout=20000)
        page.wait_for_selector("svg", timeout=20000)
        svg = page.eval_on_selector("svg", "el => el.outerHTML")
        if not svg:
            raise RuntimeError("pre 経由の描画結果が空です")
        return svg
    finally:
        try:
            page.close()
        except Exception:
            pass
        try:
            os.remove(tmp_html_path)
        except Exception:
            pass


def render_all_mermaid(diagrams, browser, use_cache, step_label):
    """diagrams: hash -> source。hash -> {ok, width_mm, from_cache, rel_path} を返す。"""
    results = {}
    if not diagrams:
        return results

    os.makedirs(IMG_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)

    total = len(diagrams)
    cache_hits = 0
    rendered_ok = 0
    failed = 0
    done = 0

    pending = []
    for h, src in diagrams.items():
        cache_path = os.path.join(CACHE_DIR, h + ".svg")
        dest_path = os.path.join(IMG_DIR, "mermaid_" + h + ".svg")
        if use_cache and os.path.isfile(cache_path):
            shutil.copy2(cache_path, dest_path)
            with open(dest_path, encoding="utf-8") as f:
                width_mm, _ = _compute_svg_physical_size(f.read())
            results[h] = {
                "ok": True,
                "width_mm": width_mm,
                "from_cache": True,
                "rel_path": "images/mermaid_" + h + ".svg",
            }
            cache_hits += 1
            done += 1
        else:
            pending.append((h, src))

    if cache_hits and not pending:
        log("%s 図を描画しています (%d/%d, キャッシュ %d)" % (step_label, done, total, cache_hits))
        return results
    if cache_hits and pending:
        log("%s キャッシュ %d 個を再利用し、残り %d 個を描画します" % (
            step_label, cache_hits, len(pending),
        ))

    page = None
    if pending and browser is not None:
        try:
            page = _get_mermaid_page(browser)
        except Exception as e:
            log("  ! ローカルの Mermaid 描画ページを開けませんでした: " + str(e))
            log_verbose(repr(e))
            page = None

    if pending and page is None:
        log("  ローカル描画が使えないため、未キャッシュの図は mermaid.ink に送ります")

    for h, src in pending:
        dest_path = os.path.join(IMG_DIR, "mermaid_" + h + ".svg")
        cache_path = os.path.join(CACHE_DIR, h + ".svg")
        width_mm = None
        ok = False
        local_err = None

        if page is not None:
            try:
                svg_text = _render_one_local(page, "mmd-" + h, src)
                width_mm = _save_svg_with_size(svg_text, dest_path)
                shutil.copy2(dest_path, cache_path)
                ok = True
                rendered_ok += 1
            except Exception as e:
                local_err = e
                log_verbose("  mermaid.render 失敗 %s: %s" % (h, e))
                try:
                    svg_text = _render_one_local_via_pre(browser, src)
                    width_mm = _save_svg_with_size(svg_text, dest_path)
                    shutil.copy2(dest_path, cache_path)
                    ok = True
                    rendered_ok += 1
                    local_err = None
                except Exception as e2:
                    local_err = e2
                    log("  ! 図 %s のローカル描画に失敗: %s" % (h, _short_err(e2)))

        if not ok:
            if local_err is not None:
                log("  ! 図 %s は mermaid.ink に切り替えます" % h)
            rel, width_mm = _render_mermaid_via_service(src)
            if rel:
                produced = os.path.join(IMG_DIR, os.path.basename(rel))
                if produced.endswith(".svg") and os.path.isfile(produced):
                    shutil.copy2(produced, cache_path)
                    dest_path = produced
                results[h] = {
                    "ok": True,
                    "width_mm": width_mm,
                    "from_cache": False,
                    "rel_path": rel,
                }
                rendered_ok += 1
                ok = True
            else:
                results[h] = {
                    "ok": False,
                    "width_mm": None,
                    "from_cache": False,
                    "rel_path": None,
                }
                failed += 1

        if ok and h not in results:
            results[h] = {
                "ok": True,
                "width_mm": width_mm,
                "from_cache": False,
                "rel_path": "images/mermaid_" + h + ".svg",
            }

        done += 1
        if done == total or done % 10 == 0 or not ok:
            log("%s 図を描画しています (%d/%d, キャッシュ %d)" % (
                step_label, done, total, cache_hits,
            ))

    if failed:
        log("  図の描画に失敗: %d / %d" % (failed, total))
    return results


# ---------------------------------------------------------------------------
# 章 HTML の整形
# ---------------------------------------------------------------------------

def clean_chapter_html(html):
    """Zenn の装飾を落とし、Mermaid はハッシュ付き <img> にする。

    Returns:
        (cleaned_html, diagrams_dict)  diagrams は hash -> source
    """
    soup = BeautifulSoup(html, "html.parser")
    diagrams = {}

    for a in soup.select("a.header-anchor-link"):
        a.decompose()

    for span in soup.select("span.zenn-embedded-mermaid"):
        iframe = span.find("iframe")
        if iframe and iframe.get("data-content"):
            mermaid_src = unquote(iframe["data-content"])
            h = mermaid_hash(mermaid_src)
            diagrams[h] = _strip_mermaid_directives(mermaid_src).strip()
            img_tag = soup.new_tag("img", src="images/mermaid_" + h + ".svg", alt="diagram")
            img_tag["data-mermaid-hash"] = h
            span.replace_with(img_tag)
        else:
            note = soup.new_tag("p")
            note.string = NOTE_DIAGRAM_FAIL
            span.replace_with(note)

    for span in soup.select("span.zenn-embedded"):
        if span.select_one("iframe"):
            note = soup.new_tag("p")
            note.string = NOTE_EMBED_FAIL
            span.replace_with(note)

    for aside in soup.select("aside.msg"):
        aside.name = "blockquote"
        for sym in aside.select(".msg-symbol"):
            sym.decompose()

    for img in soup.select("img"):
        src = img.get("src")
        if src and src.startswith("http"):
            img["src"] = download_image(src)

    return str(soup), diagrams


def apply_diagram_results(html, results):
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.select("img[data-mermaid-hash]"):
        h = img.get("data-mermaid-hash")
        info = results.get(h) if results else None
        if not info or not info.get("ok"):
            note = soup.new_tag("p")
            note.string = NOTE_DIAGRAM_FAIL
            img.replace_with(note)
            continue
        rel = info.get("rel_path") or ("images/mermaid_" + h + ".svg")
        img["src"] = rel
        if info.get("width_mm"):
            img["style"] = "width: " + ("%.1f" % info["width_mm"]) + "mm; max-width: 100%;"
        del img["data-mermaid-hash"]
    return str(soup)


def html_to_markdown(html):
    from markdownify import markdownify as md
    return md(html, heading_style="ATX", bullets="-")


# ---------------------------------------------------------------------------
# 章の取得（並列。Mermaid 描画はしない）
# ---------------------------------------------------------------------------

def process_chapter(meta, username, book_slug, fallback_position):
    slug = meta.get("slug")
    position = meta.get("position") or fallback_position
    cid = meta.get("id")
    try:
        if cid:
            ch = fetch_chapter_by_id(cid)
        else:
            log_verbose("  章 '%s' に数値 id が無いのでページ取得に切り替えます" % slug)
            ch = fetch_chapter_by_slug(username, book_slug, slug)
    except Exception as e:
        return {"ok": False, "position": position, "slug": slug, "error": str(e)}

    title = ch.get("title") or slug
    html = ch.get("body_html", "") or ""
    short = len(html) < 200
    cleaned_html, diagrams = clean_chapter_html(html)
    return {
        "ok": True,
        "position": position,
        "slug": slug,
        "title": title,
        "html": cleaned_html,
        "diagrams": diagrams,
        "short": short,
        "html_len": len(html),
    }


# ---------------------------------------------------------------------------
# 結合 HTML
# ---------------------------------------------------------------------------

def build_book_html(book_title, book_subtitle, author, cover_image_local, chapters):
    import markdown as mdlib

    toc_items = []
    sections = []
    for i, ch in enumerate(chapters):
        position = ch["position"]
        title = ch["title"]
        markdown_body = ch["markdown_body"]
        anchor = "chapter-" + str(position)
        toc_items.append(
            '<li><a href="#' + anchor + '">第' + str(position) + "章 " + str(title) + "</a></li>"
        )
        html_body = mdlib.markdown(markdown_body, extensions=["tables", "fenced_code"])
        section_class = "chapter-section first-chapter" if i == 0 else "chapter-section"
        sections.append(
            '<section id="' + anchor + '" class="' + section_class + '"><h1>第'
            + str(position) + "章 " + str(title) + "</h1>" + html_body + "</section>"
        )

    cover_subtitle_html = ("<p>" + book_subtitle + "</p>") if book_subtitle else ""
    cover_author_html = ("<p>" + author + "</p>") if author else ""
    cover_image_html = (
        '<img src="' + cover_image_local + '" class="cover-image" alt="cover" />'
        if cover_image_local else ""
    )
    cover_image_page_html = (
        '<div class="cover-image-page">' + cover_image_html + "</div>\n\n"
        if cover_image_local else ""
    )

    css = (
        "  @page { size: A4; margin: 20mm 18mm; }\n"
        "  body {\n"
        '    font-family: "Noto Sans CJK JP", "Hiragino Sans", "Yu Gothic", sans-serif;\n'
        "    font-size: 11pt;\n"
        "    line-height: 1.75;\n"
        "    color: #222;\n"
        "  }\n"
        "  h1 { font-size: 1.6em; margin-top: 2em; }\n"
        "  h2 { font-size: 1.3em; margin-top: 1.5em; }\n"
        "  h3 { font-size: 1.1em; }\n"
        "  pre {\n"
        "    background: #f5f5f5;\n"
        "    padding: 0.8em;\n"
        "    white-space: pre-wrap;\n"
        "    overflow-wrap: anywhere;\n"
        "    font-size: 0.85em;\n"
        "  }\n"
        "  code { background: #f0f0f0; padding: 0.1em 0.3em; }\n"
        "  img, svg, video { max-width: 100%; height: auto; display: block; margin: 0.8em auto; }\n"
        "  table { width: 100%; border-collapse: collapse; margin: 1em 0; }\n"
        "  th, td { border: 1px solid #ccc; padding: 0.4em 0.6em; font-size: 0.9em; }\n"
        "  blockquote { border-left: 4px solid #999; padding-left: 1em; color: #555; margin-left: 0; }\n"
        "  .toc { page-break-after: always; break-after: page; }\n"
        "  .cover { text-align: center; margin-top: 30%; page-break-after: always; break-after: page; }\n"
        "  .cover h1 { page-break-before: avoid; break-before: avoid; font-size: 2em; }\n"
        "  .cover-image-page { text-align: center; padding-top: 15%; page-break-after: always; break-after: page; }\n"
        "  .chapter-section { page-break-before: always; break-before: page; }\n"
        "  .first-chapter { page-break-before: avoid; break-before: avoid; }\n"
        "  .chapter-section h1 { margin-top: 0; }\n"
        "  .cover-image { max-width: 80%; max-height: 220mm; height: auto; display: block; margin: 0 auto; }\n"
    )

    return (
        "<!DOCTYPE html>\n"
        '<html lang="ja">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        "<title>" + str(book_title) + "</title>\n"
        "<style>\n" + css + "</style>\n"
        "</head>\n"
        "<body>\n\n"
        + cover_image_page_html
        + '<div class="cover">\n'
        "  <h1>" + str(book_title) + "</h1>\n"
        "  " + cover_subtitle_html + "\n"
        "  " + cover_author_html + "\n"
        "</div>\n\n"
        '<div class="toc">\n'
        "  <h2>目次</h2>\n"
        "  <ul>\n" + "".join(toc_items) + "\n  </ul>\n"
        "</div>\n\n"
        + "".join(sections)
        + "\n\n</body>\n</html>\n"
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _load_pdf_estimate(html_path):
    heuristic = 90.0
    try:
        size_mb = os.path.getsize(html_path) / (1024.0 * 1024.0)
        n_img = 0
        img_dir = os.path.join(os.path.dirname(html_path), "images")
        if os.path.isdir(img_dir):
            n_img = len([
                n for n in os.listdir(img_dir)
                if os.path.isfile(os.path.join(img_dir, n))
            ])
        heuristic = 20.0 + size_mb * 50.0 + n_img * 0.35
    except Exception:
        pass
    try:
        with open(PDF_ESTIMATE_PATH, encoding="utf-8") as f:
            last = float(f.read().strip())
        if 10.0 <= last <= 600.0:
            return 0.7 * last + 0.3 * heuristic
    except Exception:
        pass
    return heuristic


def _save_pdf_estimate(sec):
    try:
        os.makedirs(os.path.dirname(PDF_ESTIMATE_PATH), exist_ok=True)
        with open(PDF_ESTIMATE_PATH, "w", encoding="utf-8") as f:
            f.write("%.1f\n" % sec)
    except Exception:
        pass


def _wait_images_with_bar(page, timeout_ms):
    deadline = time.monotonic() + timeout_ms / 1000.0
    last = (-1, -1)
    while time.monotonic() < deadline:
        info = page.evaluate(
            """() => {
                const imgs = Array.from(document.images);
                const done = imgs.filter(i => i.complete).length;
                return {total: imgs.length, done};
            }"""
        )
        total = int(info.get("total") or 0)
        done = int(info.get("done") or 0)
        if total == 0:
            _end_status("  画像はありません")
            return
        if done >= total:
            _end_status(
                "  画像読み込み [%s] %d/%d (100%%)" % (
                    render_bar(1.0), total, total,
                )
            )
            return
        if (done, total) != last:
            ratio = done / float(total)
            _write_status(
                "  画像読み込み [%s] %d/%d (%d%%)" % (
                    render_bar(ratio), done, total, int(ratio * 100),
                )
            )
            last = (done, total)
        time.sleep(0.12)
    raise TimeoutError("画像の読み込みがタイムアウトしました")


class PdfConvertProgress:
    """page.pdf() / ebook-convert は進捗を返さないので、経過時間から見積もりバーを出す。"""

    def __init__(self, estimate_sec, label="変換中"):
        self.estimate = max(float(estimate_sec), 15.0)
        self.label = label
        self._stop = threading.Event()
        self._thread = None
        self._t0 = None
        self._last_pipe_log = -999.0

    def start(self):
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.wait(0.25):
            self._draw(final=False)

    def _draw(self, final):
        elapsed = time.monotonic() - self._t0
        if final:
            frac = 1.0
        else:
            tau = self.estimate / 1.6
            frac = min(1.0 - math.exp(-elapsed / tau), 0.97)
        extra = "%s経過" % format_elapsed(elapsed)
        remain = self.estimate - elapsed
        if not final and remain > 5 and frac < 0.9:
            extra += " / 残り約%s" % format_elapsed(remain)
        msg = "  %s [%s] %d%%  %s" % (
            self.label, render_bar(frac), int(round(frac * 100)), extra,
        )
        if _stdout_is_tty() or final:
            if final:
                _end_status(msg)
            else:
                _write_status(msg)
        elif elapsed - self._last_pipe_log >= 10:
            log(msg)
            self._last_pipe_log = elapsed

    def finish(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        elapsed = time.monotonic() - self._t0 if self._t0 else 0.0
        self._draw(final=True)
        return elapsed

    def abort(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
        _end_status()


def html_to_pdf(browser, html_path, pdf_path):
    html_url = Path(html_path).resolve().as_uri()
    page = browser.new_page()
    progress = None
    try:
        log("  HTML を読み込んでいます...")
        page.goto(html_url, wait_until="domcontentloaded", timeout=PDF_TIMEOUT_MS)
        _wait_images_with_bar(page, PDF_TIMEOUT_MS)
        page.wait_for_timeout(300)
        estimate = _load_pdf_estimate(html_path)
        log("  Chromium で PDF に変換しています（目安 %s）..." % format_elapsed(estimate))
        progress = PdfConvertProgress(estimate, label="PDF変換中")
        progress.start()
        page.pdf(
            path=str(Path(pdf_path).resolve()),
            format="A4",
            print_background=True,
            prefer_css_page_size=True,
            display_header_footer=False,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        elapsed = progress.finish()
        progress = None
        _save_pdf_estimate(elapsed)
    except Exception:
        if progress is not None:
            progress.abort()
        raise
    finally:
        try:
            page.close()
        except Exception:
            pass


def find_ebook_convert():
    env = os.environ.get("EBOOK_CONVERT") or os.environ.get("CALIBRE_CONVERT")
    if env:
        env = os.path.expandvars(env)
        if os.path.isfile(env):
            return env
    for name in ("ebook-convert", "ebook-convert.exe"):
        found = shutil.which(name)
        if found:
            return found
    home = os.path.expanduser("~")
    local = os.environ.get("LOCALAPPDATA") or ""
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    pf86 = os.environ.get("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    candidates = [
        os.path.join(pf, "Calibre2", "ebook-convert.exe"),
        os.path.join(pf, "Calibre", "ebook-convert.exe"),
        os.path.join(pf86, "Calibre2", "ebook-convert.exe"),
        os.path.join(local, "Programs", "Calibre2", "ebook-convert.exe"),
        os.path.join(local, "calibre-portable", "Calibre", "ebook-convert.exe"),
        "/usr/bin/ebook-convert",
        "/opt/calibre/ebook-convert",
        "/Applications/calibre.app/Contents/MacOS/ebook-convert",
        os.path.join(home, "Applications", "calibre.app", "Contents", "MacOS", "ebook-convert"),
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    return None


def calibre_install_hint():
    return (
        "EPUB の生成には Calibre の ebook-convert が必要です。\n"
        "  Windows: winget install --id calibre.calibre -e\n"
        "  または https://calibre-ebook.com/download からインストール\n"
        "入れたあとは新しいターミナルで再実行してください。\n"
        "環境変数 EBOOK_CONVERT に exe のフルパスを指定することもできます。"
    )


def metadata_from_book_html(html_path):
    title = os.path.splitext(os.path.basename(html_path))[0]
    author = ""
    cover = None
    try:
        with open(html_path, encoding="utf-8") as f:
            soup = BeautifulSoup(f.read(), "html.parser")
        if soup.title and soup.title.string:
            title = soup.title.string.strip() or title
        cover_div = soup.select_one(".cover")
        if cover_div:
            ps = [p.get_text(strip=True) for p in cover_div.find_all("p") if p.get_text(strip=True)]
            if ps:
                author = ps[-1]
        img = soup.select_one("img.cover-image") or soup.select_one(".cover-image-page img")
        if img and img.get("src"):
            cover_path = os.path.join(os.path.dirname(html_path), img["src"].replace("/", os.sep))
            if os.path.isfile(cover_path):
                cover = cover_path
    except Exception:
        pass
    return title, author, cover


_EBOOK_DIAGRAM_CSS = """
  /* Kindle/Calibre は img の max-width:100% と width:N% を混ぜて
     全部 100% 幅にしてしまう。ラッパー側で割合を持つ。 */
  p.diagram-wrap {
    display: block;
    margin: 0.8em auto;
    text-align: center;
  }
  img.diagram {
    background: transparent !important;
    background-color: transparent !important;
    width: 100%;
    height: auto;
    display: inline-block;
    mix-blend-mode: multiply;
  }
"""


def _diagram_width_percent(svg_path, png_path=None):
    """PDF と同じ mm 基準で、EPUB 用の幅（コンテンツ幅に対する％）を返す。"""
    width_mm = None
    if svg_path and os.path.isfile(svg_path):
        try:
            text = Path(svg_path).read_text(encoding="utf-8")
            m = re.search(r'<svg\b[^>]*\swidth="([\d.]+)mm"', text)
            if m:
                width_mm = float(m.group(1))
            else:
                width_mm, _ = _compute_svg_physical_size(text)
        except Exception:
            width_mm = None
    if width_mm is None and png_path and os.path.isfile(png_path):
        try:
            from PIL import Image
            with Image.open(png_path) as im:
                px_w = im.size[0]
            # rasterize は device_scale_factor=2、CSS 96dpi
            width_mm = px_w / 2.0 / 96.0 * 25.4
        except Exception:
            width_mm = None
    if not width_mm or width_mm <= 0:
        return 100
    pct = int(round(min(width_mm, MAX_DIAGRAM_WIDTH_MM) / MAX_DIAGRAM_WIDTH_MM * 100.0))
    return max(12, min(100, pct))


def _rasterize_svg_to_png(page, svg_path, png_path):
    """Kindle は SVG の foreignObject（Mermaid の文字）を描かないので PNG にする。"""
    svg_text = Path(svg_path).read_text(encoding="utf-8")
    svg_text = re.sub(r"<\?xml[^?]*\?>", "", svg_text).strip()
    html = (
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<style>html,body{margin:0;padding:0;background:transparent!important;}"
        "svg{display:block;}</style></head><body>"
        + svg_text
        + "</body></html>"
    )
    page.set_content(html, wait_until="load")
    page.locator("svg").first.screenshot(
        path=str(png_path),
        type="png",
        omit_background=True,
    )


def _prepare_ebook_html(html_path, dest_html, browser):
    """電子書籍用 HTML: Mermaid SVG を透明 PNG に替え、背景が浮かない CSS を足す。"""
    with open(html_path, encoding="utf-8") as f:
        html = f.read()
    soup = BeautifulSoup(html, "html.parser")
    head = soup.find("head")
    style = soup.find("style")
    if style is not None:
        raw = style.string or ""
        raw = raw.replace(
            "img, svg, video { max-width: 100%; height: auto; display: block; margin: 0.8em auto; }",
            "img:not(.diagram), svg, video { max-width: 100%; height: auto; display: block; margin: 0.8em auto; }",
        )
        style.string = raw + "\n" + _EBOOK_DIAGRAM_CSS
    elif head is not None:
        new_style = soup.new_tag("style")
        new_style.string = _EBOOK_DIAGRAM_CSS
        head.append(new_style)

    workdir = os.path.dirname(os.path.abspath(html_path))
    targets = []
    for img in soup.find_all("img"):
        src = (img.get("src") or "").replace("\\", "/")
        if not src.lower().endswith(".svg"):
            continue
        targets.append((img, src))

    if targets:
        log("  Kindle 向けに図を PNG 化しています (0/%d)..." % len(targets))
        page = browser.new_page(
            viewport={"width": 2400, "height": 2400},
            device_scale_factor=2,
        )
        try:
            total = len(targets)
            for i, (img, src) in enumerate(targets, 1):
                svg_path = os.path.normpath(os.path.join(workdir, src))
                png_rel = os.path.splitext(src)[0] + ".png"
                png_path = os.path.normpath(os.path.join(workdir, png_rel))
                base = os.path.basename(svg_path)
                cache_png = None
                if base.startswith("mermaid_") and base.lower().endswith(".svg"):
                    cache_png = os.path.join(CACHE_DIR, base[8:-4] + ".png")
                if not os.path.isfile(png_path) and cache_png and os.path.isfile(cache_png):
                    os.makedirs(os.path.dirname(png_path), exist_ok=True)
                    shutil.copy2(cache_png, png_path)
                if not os.path.isfile(png_path) and os.path.isfile(svg_path):
                    os.makedirs(os.path.dirname(png_path), exist_ok=True)
                    try:
                        _rasterize_svg_to_png(page, svg_path, png_path)
                        if cache_png:
                            os.makedirs(os.path.dirname(cache_png), exist_ok=True)
                            shutil.copy2(png_path, cache_png)
                    except Exception as e:
                        log("  ! PNG 化に失敗: " + os.path.basename(svg_path) + " (" + _short_err(e) + ")")
                        log_verbose(repr(e))
                if os.path.isfile(png_path):
                    img["src"] = png_rel.replace("\\", "/")
                    img["alt"] = img.get("alt") or "diagram"
                    img["class"] = "diagram"
                    img["style"] = "width: 100%; height: auto;"
                    pct = _diagram_width_percent(svg_path, png_path)
                    parent = img.parent
                    if parent is not None and parent.name == "p":
                        parent["class"] = "diagram-wrap"
                        parent["style"] = "width: %d%%;" % pct
                    else:
                        wrap = soup.new_tag("p")
                        wrap["class"] = "diagram-wrap"
                        wrap["style"] = "width: %d%%;" % pct
                        img.insert_before(wrap)
                        wrap.append(img.extract())
                if i == 1 or i == total or i % 20 == 0:
                    log("  Kindle 向けに図を PNG 化しています (%d/%d)" % (i, total))
        finally:
            try:
                page.close()
            except Exception:
                pass

    os.makedirs(os.path.dirname(dest_html), exist_ok=True)
    with open(dest_html, "w", encoding="utf-8") as f:
        f.write(str(soup))


def _calibre_cli_text(text):
    """Windows の Calibre はコマンドラインを ANSI コードページで見ることがある。
    そのページに無い文字（例: タイトルの全角ダッシュ）を渡すと引数が壊れ、
    使用法だけ出して終わる。"""
    if not text:
        return None
    if os.name != "nt":
        return text
    try:
        text.encode("mbcs")
        return text
    except UnicodeError:
        return None


def html_to_ebook(html_path, dest_path, fmt, title=None, author=None, cover_path=None):
    fmt = str(fmt).lower().lstrip(".")
    if fmt != "epub":
        raise ValueError("未対応の電子書籍形式です: " + fmt + "（Kindle 向けは EPUB のみ）")
    label = fmt.upper()

    exe = find_ebook_convert()
    if not exe:
        raise RuntimeError(calibre_install_hint())

    html_path = os.path.abspath(html_path)
    dest_path = os.path.abspath(dest_path)
    workdir = os.path.dirname(html_path)

    meta_title, meta_author, meta_cover = metadata_from_book_html(html_path)
    title = title or meta_title
    author = author or meta_author
    cover_path = cover_path or meta_cover

    # 日本語ファイル名を ebook-convert に直接渡すと Windows で使用法エラーになる。
    # 同じフォルダの ASCII 名にコピーして変換し、できたら本来の名前へ移す。
    in_name = "zenn_calibre_in.html"
    out_name = "zenn_calibre_out." + fmt
    in_tmp = os.path.join(workdir, in_name)
    out_tmp = os.path.join(workdir, out_name)
    if os.path.isfile(out_tmp):
        os.remove(out_tmp)

    opened_browser = False
    browser = _browser_state.get("browser")
    if browser is None:
        browser, err = launch_browser()
        if err:
            raise RuntimeError("図を PNG 化するのに Playwright が必要です。\n" + err)
        opened_browser = True
    try:
        _prepare_ebook_html(html_path, in_tmp, browser)
    finally:
        if opened_browser:
            close_browser()

    cmd = [
        exe,
        in_name,
        out_name,
        "--language", "ja",
        "--chapter", "//h1",
        "--chapter-mark", "pagebreak",
        "--page-breaks-before", "//h1",
        "--level1-toc", "//h1",
    ]
    cmd.extend(["--epub-version", "3"])
    title_arg = _calibre_cli_text(title)
    if title_arg:
        cmd.extend(["--title", title_arg])
    author_arg = _calibre_cli_text(author)
    if author_arg:
        cmd.extend(["--authors", author_arg])
    toc_arg = _calibre_cli_text("目次")
    if toc_arg:
        cmd.extend(["--toc-title", toc_arg])
    if cover_path and os.path.isfile(cover_path):
        rel_cover = os.path.relpath(cover_path, workdir)
        if not rel_cover.startswith(".."):
            cmd.extend(["--cover", rel_cover.replace("\\", "/")])

    log_verbose("  ebook-convert: " + " ".join(cmd))
    log("  Calibre で %s に変換しています..." % label)
    progress = PdfConvertProgress(90, label=label + "変換中")
    progress.start()
    run_kwargs = {
        "cwd": workdir,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": 600,
    }
    if os.name == "nt":
        run_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = None
    try:
        proc = subprocess.run(cmd, **run_kwargs)
        if proc.returncode == 0 and os.path.isfile(out_tmp):
            progress.finish()
        else:
            progress.abort()
    except subprocess.TimeoutExpired:
        progress.abort()
        raise RuntimeError("ebook-convert がタイムアウトしました（10分）")
    except Exception:
        progress.abort()
        raise
    finally:
        try:
            os.remove(in_tmp)
        except Exception:
            pass

    if proc is None:
        raise RuntimeError("ebook-convert を実行できませんでした")
    if proc.returncode != 0 or not os.path.isfile(out_tmp):
        err = (proc.stderr or proc.stdout or "").strip()
        log_verbose(err)
        raise RuntimeError(
            "ebook-convert が失敗しました: " + _short_err(err or ("exit %d" % proc.returncode))
        )
    try:
        os.replace(out_tmp, dest_path)
    except Exception:
        shutil.copy2(out_tmp, dest_path)
        try:
            os.remove(out_tmp)
        except Exception:
            pass
    if not os.path.isfile(dest_path):
        raise RuntimeError(label + " ファイルが作られませんでした: " + dest_path)


def html_to_epub(html_path, epub_path, title=None, author=None, cover_path=None):
    html_to_ebook(html_path, epub_path, "epub", title=title, author=author, cover_path=cover_path)


def cmd_convert_only(want_pdf, want_epub=False):
    html_path = find_book_html_for_pdf(OUT_DIR)
    if not html_path:
        log("エラー: %s に結合 HTML が見つかりません。先に本の取得を実行してください。" % OUT_DIR)
        return 1
    stem = os.path.splitext(os.path.basename(html_path))[0]
    pdf_path = os.path.join(OUT_DIR, stem + ".pdf")

    ebook_jobs = []
    if want_epub:
        ebook_jobs.append("epub")

    steps = int(bool(want_pdf)) + len(ebook_jobs)
    if steps == 0:
        log("エラー: 変換する形式がありません。")
        return 1

    log("  対象: " + os.path.basename(html_path))
    step = 0
    pdf_ok = True
    ebook_ok = {fmt: True for fmt in ebook_jobs}

    if want_pdf:
        step += 1
        log("[%d/%d] PDF を生成しています" % (step, steps))
        browser, err = launch_browser()
        if err:
            log(err)
            pdf_ok = False
        else:
            try:
                html_to_pdf(browser, html_path, pdf_path)
            except Exception as e:
                log("PDF の生成に失敗しました: " + str(e))
                log_verbose(repr(e))
                pdf_ok = False
            finally:
                close_browser()
        if pdf_ok:
            size_mb = os.path.getsize(pdf_path) / (1024 * 1024)
            log("  PDF: " + os.path.abspath(pdf_path) + " (%.2f MB)" % size_mb)

    for fmt in ebook_jobs:
        step += 1
        label = fmt.upper()
        dest = os.path.join(OUT_DIR, stem + "." + fmt)
        log("[%d/%d] %s を生成しています" % (step, steps, label))
        try:
            html_to_ebook(html_path, dest, fmt)
            size_mb = os.path.getsize(dest) / (1024 * 1024)
            log("  %s: %s (%.2f MB)" % (label, os.path.abspath(dest), size_mb))
        except Exception as e:
            log("%s の生成に失敗しました:" % label)
            log(str(e))
            log_verbose(repr(e))
            ebook_ok[fmt] = False

    ok = (not want_pdf or pdf_ok) and all(ebook_ok.values())
    if ok:
        log("完了")
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    global _VERBOSE
    args = parse_args(argv)
    _VERBOSE = args.verbose
    configure_paths(args.output_dir)

    t0 = time.monotonic()

    if args.pdf_only or args.epub_only:
        want_pdf = bool(args.pdf_only)
        want_epub = bool(args.epub_only or args.epub) and not args.no_epub
        return cmd_convert_only(want_pdf, want_epub)

    start_url = args.url or DEFAULT_URL
    html_only = args.html_only
    need_pdf = not html_only
    need_epub = bool(args.epub) and not args.no_epub
    steps = 4 + int(need_pdf) + int(need_epub)

    if not args.url:
        log("URL が無いのでデフォルトの本を使います: " + start_url)

    username, book_slug = parse_book_url(start_url)

    shutil.rmtree(IMG_DIR, ignore_errors=True)
    shutil.rmtree(CHAPTERS_DIR, ignore_errors=True)
    clear_previous_book_files(OUT_DIR)
    os.makedirs(IMG_DIR, exist_ok=True)
    os.makedirs(CHAPTERS_DIR, exist_ok=True)

    log("[1/%d] 本の情報を取得しています..." % steps)

    book_title = None
    book_subtitle = None
    author = None
    api_data = None
    chapters_meta = []

    try:
        api_data, chapters_meta = find_chapters_via_api(book_slug)
        if not chapters_meta:
            raise ValueError("API が使える章を返しませんでした")
        book_title, book_subtitle, author = extract_book_meta(api_data, username)
        log("  API から %d 章を見つけました" % len(chapters_meta))
    except Exception as e:
        log("  ! API での章発見に失敗したので、ページ内のリンクを集めます (" + str(e) + ")")
        book_title, chapters_meta = find_chapters_via_scrape(start_url, username, book_slug)
        author = username
        log("  スクレイピングで %d 章を見つけました" % len(chapters_meta))

    if not book_title:
        book_title = fetch_page_title(start_url) or book_slug

    log("  タイトル: " + str(book_title))
    if book_subtitle:
        log("  副題: " + str(book_subtitle))
    log("  著者: " + str(author))
    log("  slug: " + book_slug)

    cover_image_local = None
    cover_image_url = extract_cover_image_url(api_data, username, book_slug)
    if cover_image_url:
        log("  表紙画像を取得します")
        cover_image_local = download_image(cover_image_url)
    else:
        log("  表紙画像はありません")

    if not chapters_meta:
        log("章が見つかりませんでした。終了します。")
        return 1

    import concurrent.futures

    total_chapters = len(chapters_meta)
    log("[2/%d] 章を取得しています (0/%d) ..." % (steps, total_chapters))

    chapters = []
    diagrams = {}
    completed = [0]
    progress_lock = threading.Lock()

    def _run_one(meta, index):
        result = process_chapter(meta, username, book_slug, index + 1)
        with progress_lock:
            completed[0] += 1
            n = completed[0]
            pos = result.get("position")
            slug = result.get("slug")
            if result.get("ok"):
                extra = ""
                if result.get("short"):
                    extra = " ※本文が短いです (%d 文字)" % result.get("html_len", 0)
                log("[2/%d] 章を取得しています (%d/%d) 第%d章 %s%s" % (
                    steps, n, total_chapters, pos, slug, extra,
                ))
            else:
                log("[2/%d] 章を取得しています (%d/%d) 第%d章 %s ... 失敗: %s" % (
                    steps, n, total_chapters, pos, slug, result.get("error"),
                ))
        return result

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(_run_one, meta, i)
            for i, meta in enumerate(chapters_meta)
        ]
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            if result.get("ok"):
                chapters.append(result)
                diagrams.update(result.get("diagrams") or {})

    chapters.sort(key=lambda x: x["position"])

    need_pdf = not html_only
    if args.no_mermaid_cache:
        uncached = bool(diagrams)
    else:
        uncached = any(
            not os.path.isfile(os.path.join(CACHE_DIR, h + ".svg"))
            for h in diagrams
        )
    need_browser = uncached or need_pdf
    browser = None
    browser_err = None
    if need_browser:
        browser, browser_err = launch_browser()
        if browser_err:
            if diagrams:
                log(browser_err)
                log("  図は mermaid.ink で描画を試みます。PDF は Playwright が必要です。")
            elif need_pdf:
                log(browser_err)
                log("  HTML までは生成しますが、PDF は作れません。")

    log("[3/%d] 図を描画しています..." % steps)
    diagram_results = {}
    if diagrams:
        diagram_results = render_all_mermaid(
            diagrams,
            browser,
            use_cache=not args.no_mermaid_cache,
            step_label="[3/%d]" % steps,
        )
    else:
        log("[3/%d] 図はありません。スキップします" % steps)

    cache_hits = sum(1 for r in diagram_results.values() if r.get("from_cache"))
    new_ok = sum(1 for r in diagram_results.values() if r.get("ok") and not r.get("from_cache"))
    failed_diagrams = sum(1 for r in diagram_results.values() if not r.get("ok"))

    book_stem = book_output_stem(book_title, book_slug)
    html_name = book_stem + ".html"
    pdf_name = book_stem + ".pdf"
    log("[4/%d] %s を書き出しています..." % (steps, html_name))
    os.makedirs(CHAPTERS_DIR, exist_ok=True)
    for ch in chapters:
        html = apply_diagram_results(ch["html"], diagram_results)
        markdown_body = html_to_markdown(html)
        ch["markdown_body"] = markdown_body
        fname = os.path.join(
            CHAPTERS_DIR,
            str(ch["position"]).zfill(2) + "-" + str(ch["slug"]) + ".md",
        )
        with open(fname, "w", encoding="utf-8") as f:
            f.write("# 第" + str(ch["position"]) + "章 " + str(ch["title"]) + "\n\n" + markdown_body + "\n")

    html_doc = build_book_html(
        book_title, book_subtitle, author, cover_image_local, chapters,
    )
    out_html = os.path.join(OUT_DIR, html_name)
    with open(out_html, "w", encoding="utf-8") as f:
        f.write(html_doc)
    abs_html = os.path.abspath(out_html)

    pdf_ok = False
    abs_pdf = None
    pdf_size_mb = None
    step = 4
    if need_pdf:
        step += 1
        log("[%d/%d] PDF を生成しています" % (step, steps))
        out_pdf = os.path.join(OUT_DIR, pdf_name)
        if browser is None:
            log("  PDF を生成できません。Playwright を導入してください:")
            log("    pip install playwright")
            log("    playwright install chromium")
            if browser_err:
                log("  " + browser_err.replace("\n", "\n  "))
        else:
            try:
                html_to_pdf(browser, out_html, out_pdf)
                pdf_ok = True
                abs_pdf = os.path.abspath(out_pdf)
                pdf_size_mb = os.path.getsize(out_pdf) / (1024 * 1024)
            except Exception as e:
                log("  PDF の生成に失敗しました: " + str(e))
                log_verbose(repr(e))

    cover_abs = None
    if cover_image_local:
        cover_abs = os.path.join(OUT_DIR, cover_image_local.replace("/", os.sep))

    ebook_results = {}
    if need_epub:
        step += 1
        log("[%d/%d] EPUB を生成しています" % (step, steps))
        dest = os.path.join(OUT_DIR, book_stem + ".epub")
        try:
            html_to_ebook(
                out_html,
                dest,
                "epub",
                title=book_title,
                author=author,
                cover_path=cover_abs,
            )
            ebook_results["epub"] = {
                "ok": True,
                "path": os.path.abspath(dest),
                "mb": os.path.getsize(dest) / (1024 * 1024),
            }
        except Exception as e:
            log("  EPUB の生成に失敗しました:")
            log("  " + str(e).replace("\n", "\n  "))
            log_verbose(repr(e))
            ebook_results["epub"] = {"ok": False}

    elapsed = format_elapsed(time.monotonic() - t0)
    log("")
    log("完了: %d/%d 章, 図 %d（キャッシュ %d / 新規 %d / 失敗 %d）" % (
        len(chapters), total_chapters, len(diagrams),
        cache_hits, new_ok, failed_diagrams,
    ))
    log("HTML: " + abs_html)
    if pdf_ok:
        log("PDF:  " + abs_pdf + " (%.2f MB)" % pdf_size_mb)
    elif need_pdf:
        log("PDF:  生成できませんでした。HTML をブラウザで開いて確認できます。")
    info = ebook_results.get("epub")
    if info and info.get("ok"):
        log("EPUB: " + info["path"] + " (%.2f MB)" % info["mb"])
    elif need_epub:
        log("EPUB: 生成できませんでした。Calibre の導入を確認してください。")
    log("所要時間: " + elapsed)
    ebook_ok = (not need_epub) or (ebook_results.get("epub") or {}).get("ok")
    return 0 if (not need_pdf or pdf_ok) and ebook_ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        close_browser()
