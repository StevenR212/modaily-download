#!/usr/bin/env python3
"""澳門日報 (Macau Daily News) 版面爬取工具 — v2

Changes from v1:
  - Page list from index.html (<ul id="list"> <li>) instead of node_A01.html sidebar
    → no longer fails when node_A01.html shows "正在製作"
  - UUID-based images downloaded directly from OSS (no need to extract from node page)
  - Handles "正在製作" node pages gracefully (skips articles, still downloads page images)
  - UUID → page_XX.jpg rename for backward compatibility with server/frontend

Usage:
  python3 crawl_modaily.py                          # 今日報紙
  python3 crawl_modaily.py --today                  # 同上
  python3 crawl_modaily.py --date 2026-06-28        # 指定一日
  python3 crawl_modaily.py --start 2026-06-01 --end 2026-06-28  # 日期範圍
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# ============================================================
# 配置
# ============================================================
# OSS 基底 URL
OSS_BASE = "https://xiangyu-macau.oss-cn-hongkong.aliyuncs.com"
LAYOUT_BASE = f"{OSS_BASE}/app/szb/pc/layout"     # 版面 HTML
PIC_BASE = f"{OSS_BASE}/app/szb/pc/pic"            # 圖片
CONTENT_BASE = f"{OSS_BASE}/app/szb/pc/content"    # 文章內容

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8",
    "Referer": "https://www.modaily.cn/",
}
REQUEST_TIMEOUT = 30
REQUEST_DELAY = 0.5  # seconds between requests (polite)

# 標記版面仍在製作中（未有文章）
IN_PRODUCTION_MARKER = "正在制作"


def log(msg: str):
    print(f"[{datetime.now():%H:%M:%S}] {msg}")


def download_file(url: str, dest_path: Path, desc: str = "") -> bool:
    """Download a file from url to dest_path. Returns True on success."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(resp.content)
        size_kb = len(resp.content) / 1024
        log(f"  ✓ {desc or url} ({size_kb:.1f} KB)")
        return True
    except requests.RequestException as e:
        log(f"  ✗ {desc or url}: {e}")
        return False


def get_soup(url: str) -> str | None:
    """Fetch HTML content. Returns text or None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return resp.text
    except requests.RequestException as e:
        log(f"  ✗ Failed to fetch {url}: {e}")
        return None


# ============================================================
# V2: Parse page list from index.html
# ============================================================

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

    Returns list of dicts: {node_id, node_file, section, image_uuid, image_path}
    Compatible with old _pages.json format (node_id, node_file, section).
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

        # image relative path (e.g. "../../../pic/202607/03/UUID.jpg.1")
        # Extract both the full path and the UUID filename
        img_match = re.search(r'src="([^"]*pic/\d+/\d+/([^/"]+))"', li_content)
        image_path = img_match.group(1) if img_match else ""
        image_uuid = img_match.group(2) if img_match else ""

        # section name: text after <br/> e.g. "第A01版 澳聞"
        section = ""
        # Try with <br/> separator first
        after_br = li_content.split("<br/>")
        if len(after_br) > 1:
            section = re.sub(r'<[^>]+>', '', after_br[-1]).strip()
            # Extract section name after "第X版："
            sec_match = re.search(r'第[A-Z]\d+版[：:]\s*(.+)', section)
            if sec_match:
                section = sec_match.group(1).strip()

        pages.append({
            "node_id": node_id,
            "node_file": node_file,
            "section": section,
            "image_uuid": image_uuid,
            "image_path": image_path,
        })

    return pages


# ============================================================
# V1 page node functions (still work for dates with content)
# ============================================================
# === Article extraction from www.macaodaily.com ===
def extract_articles_from_macaodaily(date_str: str, pages: list[dict], articles_dir: Path) -> int:
    """Extract articles from www.macaodaily.com for each newspaper section.

    OSS node pages return "正在製作" for current-day content, so we get
    articles from the live website instead. Images still come from OSS.

    Args:
        date_str: Date in YYYY-MM-DD format
        pages: List of page dicts from OSS index.html (has node_id, section)
        articles_dir: Path to articles/ directory
    Returns:
        Total number of articles extracted
    """
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    date_path = dt.strftime("%Y-%m/%d")  # e.g. 2026-07/03

    base_url = f"https://www.macaodaily.com/html/{date_path}"
    node_2_url = f"{base_url}/node_2.htm"

    # ── Step 1: Get section sidebar from first node page (A01) ──
    log(f"🌐 Fetching sections from www.macaodaily.com...")
    resp = requests.get(node_2_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    soup = BeautifulSoup(resp.text, "html.parser")

    # Build section_code → macaodaily_node_id mapping
    # Sidebar: <a id="pageLink" href="node_2.htm">第A01版：澳聞</a>
    section_map = {}  # "A01" → 2, "A02" → 3, …
    for a in soup.find_all("a", id="pageLink"):
        href = a.get("href", "")
        text = a.get_text(strip=True)
        m = re.search(r"node_(\d+)\.htm", href)
        sm = re.search(r"第([A-Z]\d+)版", text)
        if m and sm:
            section_map[sm.group(1)] = int(m.group(1))

    log(f"  Found {len(section_map)} sections on macaodaily.com")

    # Build OSS section_code → page dict mapping
    # OSS section text: "第A01版 澳聞" or "第A01版 澳聞/特刋"
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
            log(f"  ⏭️  {section_code} not in OSS pages, skipping")
            continue

        page = oss_page_map[section_code]
        node_id = page["node_id"]
        section_name = page["section"]

        node_url = f"{base_url}/node_{macao_node_id}.htm"
        log(f"📄 {node_id} - {section_name}: fetching node page")

        resp = requests.get(node_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        node_soup = BeautifulSoup(resp.text, "html.parser")

        # Extract article links (deduplicate — page has each link twice:
        # once in the image map area, once in the text list below)
        article_links = []
        seen = set()
        for a in node_soup.find_all("a", href=re.compile(r"^content_\d+\.htm")):
            href = a.get("href", "")
            if href not in seen:
                seen.add(href)
                article_links.append(href)

        if not article_links:
            log(f"  📝 No articles found")
            continue

        log(f"  📝 {len(article_links)} article(s)")

        for j, art_path in enumerate(article_links):
            m = re.search(r"content_(\d+)\.htm", art_path)
            art_id = m.group(1) if m else f"art_{j+1}"
            art_url = f"{base_url}/{art_path}"

            # Fetch article page
            resp = requests.get(art_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            art_soup = BeautifulSoup(resp.text, "html.parser")

            # Extract title from <strong style="font-size:23px">
            title_tag = art_soup.select_one('strong[style*="font-size:23px"]')
            title = title_tag.get_text(strip=True) if title_tag else ""

            # Extract body from <founder-content>
            body_text = ""
            founder = art_soup.find("founder-content")
            if founder:
                paras = []
                for p in founder.find_all("p"):   # handles both <P> and <p>
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
                "section": section_name,
                "date": date_str,
                "url": art_url,
                "body_text": body_text,
                "body_length": len(body_text),
            }
            art_meta_path = page_art_dir / f"{art_id}.json"
            art_meta_path.write_text(json.dumps(art_meta, ensure_ascii=False, indent=2))

            # Save raw HTML too (for future re-parsing)
            art_html_path = page_art_dir / f"{art_id}.html"
            art_html_path.write_text(resp.text)

            log(f"  ✓ {j+1}. {title[:60]}")
            total_articles += 1

            time.sleep(REQUEST_DELAY)

        time.sleep(REQUEST_DELAY)

    log(f"  📰 Total: {total_articles} articles extracted from www.macaodaily.com")
    return total_articles



# Helper: build UUID image URL from relative path in index.html
# ============================================================

def build_image_url_from_path(rel_path: str, date_str: str) -> str:
    """
    Resolve a relative image path like:
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


def has_content(html: str) -> bool:
    """Check if a node page has real content (not '正在製作' placeholder)."""
    return IN_PRODUCTION_MARKER not in html


# ============================================================
# Main crawl logic
# ============================================================

def crawl_newspaper(date_str: str, output_dir: Path):
    """Crawl Macau Daily News for a specific date."""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    yyyymm = dt.strftime("%Y%m")
    dd = dt.strftime("%d")
    date_path = f"{yyyymm}/{dd}"

    day_dir = output_dir / date_str
    day_dir.mkdir(parents=True, exist_ok=True)

    log(f"📰 澳門日報 {date_str}")

    # ── Step 1: Fetch index.html to get page list ──
    index_url = f"{LAYOUT_BASE}/{date_path}/index.html"
    log(f"🌐 Fetching page list: {LAYOUT_BASE}/{date_path}/index.html")

    index_html = get_soup(index_url)
    if not index_html:
        log(f"❌ Cannot access index page for {date_str}. Date may not exist.")
        return

    pages = extract_pages_from_index(index_html)
    if not pages:
        log("⚠️  No pages found in index.html — newspaper format may have changed.")
        log(f"📄 First 500 chars of response:")
        log(index_html[:500])
        return

    log(f"📑 Found {len(pages)} pages:")
    for p in pages:
        log(f"    {p['node_id']} - {p['section']}")

    # Save page list as JSON (compatible format with v1)
    page_list_path = day_dir / "_pages.json"
    # For _pages.json, only save the fields the server/frontend needs
    simple_pages = [
        {"node_id": p["node_id"], "node_file": p["node_file"], "section": p["section"]}
        for p in pages
    ]
    page_list_path.write_text(json.dumps(simple_pages, ensure_ascii=False, indent=2))
    log(f"📋 Page list saved: {page_list_path}")

    # ── Step 2: Crawl each page ──
    articles_dir = day_dir / "articles"
    pages_dir = day_dir / "pages"

    for i, page in enumerate(pages):
        node_id = page["node_id"]
        section = page["section"]
        node_url = f"{LAYOUT_BASE}/{date_path}/{page['node_file']}"

        log(f"\n📄 [{i+1}/{len(pages)}] Page {node_id} - {section}")

        # ── Download page image ──
        # Use UUID image URL from index.html, save as page_A01.jpg for compat
        img_url = build_image_url_from_path(page["image_path"], date_str)
        if img_url:
            img_name = f"page_{node_id}.jpg"
            img_path = pages_dir / img_name
            download_file(img_url, img_path, f"Page image: {node_id}")
        else:
            log(f"  ⚠️  No UUID image in index — 寧缺勿濫")

        # ── Check node page status ──
        page_html = get_soup(node_url)
        if not page_html:
            log(f"  ⚠️  Cannot fetch node page")
        else:
            # Info log: OSS node pages return "正在製作" for current dates
            if not has_content(page_html):
                log(f"  ⏳ OSS '正在製作' — 文章將從 www.macaodaily.com 提取")
            else:
                log(f"  ✓ OSS has content — articles also available from www.macaodaily.com")

        time.sleep(REQUEST_DELAY)

    # ── Step 3: Extract articles from www.macaodaily.com ──
    log(f"\n🌐 Extracting articles from www.macaodaily.com ...")
    article_count = extract_articles_from_macaodaily(date_str, pages, articles_dir)

    # ── Generate summary ──
    summary = {
        "newspaper": "澳門日報 (Macau Daily News)",
        "date": date_str,
        "total_pages": len(pages),
        "total_articles": article_count,
        "pages": [{"id": p["node_id"], "section": p["section"]} for p in pages],
        "output_dir": str(day_dir),
    }
    summary_path = day_dir / "_summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    log(f"\n✅ Done! Summary saved: {summary_path}")
    log(f"📁 Output: {day_dir}")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="澳門日報版面爬取工具 (Macau Daily News Scraper) — v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\n使用範例：
  python3 crawl_modaily.py                          # 今日報紙
  python3 crawl_modaily.py --today                  # 同上
  python3 crawl_modaily.py --date 2026-06-28        # 指定一日
  python3 crawl_modaily.py --start 2026-06-01       # 指定日期起至今日
  python3 crawl_modaily.py --start 2026-06-01 --end 2026-06-28  # 日期範圍
        """,
    )
    parser.add_argument("--date", type=str, help="單日：要爬取嘅日期 (YYYY-MM-DD)")
    parser.add_argument("--start", type=str, help="開始日期 (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, help="結束日期 (YYYY-MM-DD)，唔填就係今日")
    parser.add_argument("--today", action="store_true", help="爬今日報紙")
    parser.add_argument("--output", type=str, default="/root/modaily_output",
                        help="輸出目錄 (default: /root/modaily_output)")

    args = parser.parse_args()

    # Determine dates to crawl
    today = date.today()

    if args.date:
        dates = [args.date]
    elif args.start:
        start_dt = datetime.strptime(args.start, "%Y-%m-%d").date()
        end_dt = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else today
        if start_dt > end_dt:
            log(f"❌ 開始日期 ({args.date}) 遲過結束日期 ({args.end or today})")
            sys.exit(1)
        dates = []
        d = start_dt
        while d <= end_dt:
            dates.append(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)
    else:
        # 預設：今日
        dates = [today.strftime("%Y-%m-%d")]

    output_dir = Path(args.output)
    total = len(dates)

    log(f"📅 將會爬取 {total} 日嘅澳門日報\n")

    for i, ds in enumerate(dates, 1):
        log(f"═══ [{i}/{total}] {ds} ═══")
        crawl_newspaper(ds, output_dir)
        if i < total:
            log("")

    log(f"\n🎉 全部完成！共爬取 {total} 日報紙")
    log(f"📁 輸出目錄：{output_dir.absolute()}")


if __name__ == "__main__":
    main()
