#!/usr/bin/env python3
"""
澳門日報 (Macau Daily News) 版面爬取工具 — 靜音版
===================================================
功能完全同原版，僅以最簡潔輸出顯示進度（無每篇文章/每張圖的明細）。
適合大量日期批次爬取，減少 terminal I/O 開銷。

文章內容從 www.macaodaily.com 提取 (<founder-content>)，頁面圖片繼續用 OSS（質素較高）。

Usage:
  python3 crawl_modaily_quiet.py                          # 今日報紙
  python3 crawl_modaily_quiet.py --today                  # 同上
  python3 crawl_modaily_quiet.py --date 2026-06-28        # 指定一日
  python3 crawl_modaily_quiet.py --start 2026-06-01       # 指定日期起至今日
  python3 crawl_modaily_quiet.py --start 2026-06-01 --end 2026-06-28  # 日期範圍
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# Marker for OSS placeholder pages
IN_PRODUCTION_MARKER = "正在制作"

# ============================================================
# 配置（同原版完全一致）
# ============================================================
OSS_BASE = "https://xiangyu-macau.oss-cn-hongkong.aliyuncs.com"
LAYOUT_BASE = f"{OSS_BASE}/app/szb/pc/layout"
PIC_BASE = f"{OSS_BASE}/app/szb/pc/pic"
CONTENT_BASE = f"{OSS_BASE}/app/szb/pc/content"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8",
    "Referer": "https://www.modaily.cn/",
}

REQUEST_TIMEOUT = 30
REQUEST_DELAY = 0.3


# ── Counters（全域進度） ──
class Stats:
    def __init__(self):
        self.dates_total = 0
        self.dates_done = 0
        self.dates_skipped = 0
        self.pages_total = 0
        self.articles_total = 0
        self.images_ok = 0
        self.articles_ok = 0
        self.errors = 0


_stats = Stats()
_stats_lock = threading.Lock()


def safe_filename(text: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', '_', text).strip()


def download_file(url: str, dest_path: Path) -> bool:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        return True
    except requests.RequestException:
        return False


def get_soup(url: str) -> str | None:
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.text
    except requests.RequestException:
        return None


def extract_pages_from_index(html: str) -> list[dict]:
    """
    Parse index.html (<ul id="list"> <li>) to extract all page nodes.

    Each <li> looks like:
        <li>
            <a href="node_A01.html" target="_blank">
                <img src="../../../pic/YYYYMM/DD/UUID.jpg.1"/>
                <br/>
                第A01版 澳聞
            </a>
        </li>

    Returns list of dicts: {node_id, node_file, section, image_path}
    Compatible with old _pages.json format.
    """
    pages = []
    li_pattern = re.compile(r'<li[^>]*>(.*?)</li>', re.DOTALL)

    for li_match in li_pattern.finditer(html):
        li_content = li_match.group(1)

        # node_file (e.g. "node_A01.html") and node_id (e.g. "A01")
        node_match = re.search(r'href="(node_([A-Z]\d+)\.html)"', li_content)
        if not node_match:
            continue
        node_file = node_match.group(1)
        node_id = node_match.group(2)

        # image_path (relative path like ../../../pic/202607/02/UUID.jpg.1)
        img_match = re.search(r'<img[^>]+src="([^"]+)"', li_content)
        image_path = img_match.group(1) if img_match else ""

        # section name: text after <br/> e.g. "第A01版 澳聞"
        section = ""
        after_br = li_content.split("<br/>")
        if len(after_br) > 1:
            section = re.sub(r'<[^>]+>', '', after_br[-1]).strip()
            sec_match = re.search(r'第[A-Z]\d+版[：:]\s*(.+)', section)
            if sec_match:
                section = sec_match.group(1).strip()

        pages.append({
            "node_id": node_id,
            "node_file": node_file,
            "section": section,
            "image_path": image_path,
        })

    return pages


def extract_page_image_abs(html: str, page_url: str) -> str | None:
    match = re.search(r'<img[^>]*class="preview"[^>]*src="([^"]+)"', html)
    if match:
        return urljoin(page_url, match.group(1))
    return None


def extract_articles_from_macaodaily(date_str: str, pages: list[dict],
                                     articles_dir: Path) -> int:
    """
    Extract articles from www.macaodaily.com for each newspaper section.

    OSS node pages return "正在製作" for current-day content, so we get
    articles from the live website instead. Images still come from OSS.

    Quiet version — minimal output.
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_path = dt.strftime("%Y-%m/%d")  # e.g. 2026-07/03

    base_url = f"https://www.macaodaily.com/html/{date_path}"
    node_2_url = f"{base_url}/node_2.htm"

    # ── Step 1: Get section sidebar from first node page ──
    resp = requests.get(node_2_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    # Build section_code → macaodaily_node_id mapping
    section_map = {}  # "A01" → 2, "A02" → 3, …
    for a in soup.find_all("a", id="pageLink"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        m = re.search(r"node_(\d+)\.htm", href)
        sm = re.search(r"第([A-Z]\d+)版", text)
        if m and sm:
            section_map[sm.group(1)] = int(m.group(1))

    # Build OSS section_code → page dict mapping
    oss_page_map = {}
    for p in pages:
        section_text = p.get("section", "")
        m = re.search(r"第([A-Z]\d+)版", section_text)
        if m:
            oss_page_map[m.group(1)] = p

    total_articles = 0

    # ── Step 2: For each matching section, extract articles ──
    for section_code, macao_node_id in section_map.items():
        if section_code not in oss_page_map:
            continue

        page = oss_page_map[section_code]
        node_id = page["node_id"]

        node_url = f"{base_url}/node_{macao_node_id}.htm"

        resp = requests.get(node_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        node_soup = BeautifulSoup(resp.text, "html.parser")

        # Extract article links (deduplicate)
        article_links = []
        seen = set()
        for a in node_soup.find_all("a", href=re.compile(r"^content_\d+\.htm")):
            href = a.get("href", "")
            if href not in seen:
                seen.add(href)
                article_links.append(href)

        if not article_links:
            continue

        for j, art_path in enumerate(article_links):
            m = re.search(r"content_(\d+)\.htm", art_path)
            art_id = m.group(1) if m else f"art_{j+1}"
            art_url = f"{base_url}/{art_path}"

            # Fetch article page
            resp = requests.get(art_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            art_soup = BeautifulSoup(resp.text, "html.parser")

            # Extract title
            title_tag = art_soup.select_one('strong[style*="font-size:23px"]')
            title = title_tag.get_text(strip=True) if title_tag else ""

            # Extract body from <founder-content>
            body_text = ""
            founder = art_soup.find("founder-content")
            if founder:
                paras = []
                for p in founder.find_all("p"):
                    text = p.get_text(strip=True)
                    if text:
                        paras.append(text)
                body_text = "\n".join(paras)

            # Save article
            page_art_dir = articles_dir / f"page_{node_id}"
            page_art_dir.mkdir(parents=True, exist_ok=True)

            art_meta = {
                "id": art_id,
                "title": title,
                "page": node_id,
                "section": page["section"],
                "date": date_str,
                "url": art_url,
                "body_text": body_text,
                "body_length": len(body_text),
            }
            (page_art_dir / f"{art_id}.json").write_text(
                json.dumps(art_meta, ensure_ascii=False, indent=2))
            (page_art_dir / f"{art_id}.html").write_text(resp.text)

            total_articles += 1
            time.sleep(REQUEST_DELAY)

        time.sleep(REQUEST_DELAY)

    return total_articles


def build_image_url_from_path(rel_path: str, date_str: str) -> str:
    """
    Convert relative image path from index.html e.g.
    ../../../pic/202606/29/UUID.jpg.1
    to a full OSS URL:
        {PIC_BASE}/202606/29/UUID.jpg.1

    Falls back to extracting UUID from path.
    """
    if not rel_path:
        return ""
    if rel_path.startswith("http"):
        return rel_path

    # Extract just the filename (UUID.jpg or UUID.jpg.1)
    m = re.search(r'pic/\d+/\d+/([^/"]+)', rel_path)
    if not m:
        return ""
    uuid_file = m.group(1)

    yy = date_str[:4]
    mm = date_str[5:7]
    dd = date_str[8:10]
    return f"{PIC_BASE}/{yy}{mm}/{dd}/{uuid_file}"


def _process_page(page: dict, date_str: str, date_path: str,
                  articles_dir: Path, pages_dir: Path):
    """Process a single page (fetch image only — articles via macaodaily.com)."""
    node_id = page["node_id"]
    node_url = f"{LAYOUT_BASE}/{date_path}/{page['node_file']}"

    local_images = 0

    # Build image URL from the image_path extracted from index.html
    img_url = build_image_url_from_path(page.get("image_path", ""), date_str)
    if img_url:
        img_path = pages_dir / f"page_{node_id}.jpg"
        if download_file(img_url, img_path):
            local_images += 1

    time.sleep(REQUEST_DELAY)
    return 0, local_images


def crawl_newspaper(date_str: str, output_dir: Path, parallel: int = 1):
    """Crawl one day's newspaper. Minimal output."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    yyyymm = dt.strftime("%Y%m")
    dd = dt.strftime("%d")
    date_path = f"{yyyymm}/{dd}"

    day_dir = output_dir / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    # ── Step 1: Fetch index.html to get page list ──
    index_url = f"{LAYOUT_BASE}/{date_path}/index.html"

    html = get_soup(index_url)
    if not html:
        _stats.dates_skipped += 1
        _stats.errors += 1
        return

    # Check if this date has real content (not placeholder)
    if IN_PRODUCTION_MARKER in html:
        _stats.dates_skipped += 1
        _stats.errors += 1
        return

    pages = extract_pages_from_index(html)
    if not pages:
        _stats.dates_skipped += 1
        _stats.errors += 1
        return

    _stats.pages_total += len(pages)

    # Save page list
    (day_dir / "_pages.json").write_text(json.dumps(pages, ensure_ascii=False, indent=2))

    articles_dir = day_dir / "articles"
    pages_dir = day_dir / "pages"

    # ── Step 1: Download page images from OSS ──
    if parallel <= 1:
        for page in pages:
            _, i = _process_page(page, date_str, date_path, articles_dir, pages_dir)
            with _stats_lock:
                _stats.images_ok += i
            time.sleep(REQUEST_DELAY)
    else:
        with ThreadPoolExecutor(max_workers=parallel) as pool:
            fut_map = {
                pool.submit(
                    _process_page, page, date_str, date_path,
                    articles_dir, pages_dir
                ): page
                for page in pages
            }
            for fut in as_completed(fut_map):
                _, i = fut.result()
                with _stats_lock:
                    _stats.images_ok += i

    # ── Step 2: Extract articles from www.macaodaily.com ──
    articles_count = extract_articles_from_macaodaily(date_str, pages, articles_dir)
    with _stats_lock:
        _stats.articles_ok += articles_count

    # Save summary
    summary = {
        "newspaper": "澳門日報 (Macau Daily News)",
        "date": date_str,
        "total_pages": len(pages),
        "total_articles": articles_count,
        "total_images": _stats.images_ok,
        "pages": [{"id": p["node_id"], "section": p["section"]} for p in pages],
        "output_dir": str(day_dir),
    }
    (day_dir / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="澳門日報版面爬取工具（靜音版）- Macau Daily News Scraper (Quiet)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Examples:
  python3 crawl_modaily_quiet.py                          # 今日報紙
  python3 crawl_modaily_quiet.py --date 2026-06-28        # 指定一日
  python3 crawl_modaily_quiet.py --start 2026-06-01       # 指定日期起至今日
  python3 crawl_modaily_quiet.py --start 2026-06-01 --end 2026-06-28  # 日期範圍
        """,
    )
    parser.add_argument("--date", type=str, help="單日：要爬取嘅日期 (YYYY-MM-DD)")
    parser.add_argument("--start", type=str, help="開始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="結束日期 (YYYY-MM-DD)，唔填就係今日")
    parser.add_argument("--today", action="store_true", help="爬今日報紙")
    parser.add_argument("--output", type=str, default="./modaily_output",
                        help="輸出目錄 (default: ./modaily_output)")
    parser.add_argument("--parallel", type=int, default=1,
                        help="同時下載數量 (default: 1，順序爬取)")

    args = parser.parse_args()

    today = date.today()

    if args.date:
        dates = [args.date]
    elif args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else today
        if start_dt > end_dt:
            print(f"❌ 開始日期 ({args.start}) 遲過結束日期 ({args.end or today})")
            sys.exit(1)
        dates = []
        d = start_dt
        while d <= end_dt:
            dates.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
    else:
        dates = [today.strftime("%Y-%m-%d")]

    output_dir = Path(args.output)
    _stats.dates_total = len(dates)

    t_start = time.time()

    for i, ds in enumerate(dates, 1):
        crawl_newspaper(ds, output_dir, parallel=args.parallel)
        _stats.dates_done += 1
        elapsed = time.time() - t_start
        eta = ""
        if i < len(dates):
            per_day = elapsed / i
            remaining = per_day * (len(dates) - i)
            eta = f", ETA {remaining:.0f}s"
        print(f"  [{i}/{len(dates)}] {ds} — "
              f"{_stats.articles_ok} articles, "
              f"{_stats.images_ok} images{eta}")

    total_time = time.time() - t_start
    print(f"\n✅ Done: {_stats.dates_done} days, "
          f"{_stats.articles_ok} articles, "
          f"{_stats.images_ok} images, "
          f"{_stats.dates_skipped} skipped"
          f"  ({total_time:.0f}s)")
    print(f"📁 {output_dir.absolute()}")


if __name__ == "__main__":
    main()
