#!/usr/bin/env python3
"""澳門日報本地瀏覽器 - FastAPI Backend"""

import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

# ── Config ──
OUTPUT_DIR = Path(os.environ.get("MODAILY_OUTPUT", "/root/modaily_output"))
API_PORT = int(os.environ.get("MODAILY_PORT", "5678"))

app = FastAPI(title="澳門日報瀏覽器", version="1.0")

# ── CORS (allow nginx proxy) ──
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Helpers ──

def list_date_dirs() -> list[str]:
    """Return sorted list of available dates (YYYY-MM-DD) from output dir."""
    dates = []
    if not OUTPUT_DIR.exists():
        return dates
    for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
        if d.is_dir() and (d / "_pages.json").exists():
            try:
                datetime.strptime(d.name, "%Y-%m-%d")
                dates.append(d.name)
            except ValueError:
                pass
    return dates


def load_json(path: Path) -> dict | list | None:
    try:
        return json.loads(path.read_text("utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def get_image_path(date_str: str, page_id: str) -> Path | None:
    for ext in [".jpg", ".png", ".webp"]:
        p = OUTPUT_DIR / date_str / "pages" / f"page_{page_id}{ext}"
        if p.exists():
            return p
    return None


# ── API Routes ──

@app.get("/api/dates")
def api_dates():
    """列出所有可瀏覽嘅日期"""
    dates = list_date_dirs()
    result = []
    for d in dates:
        summary = load_json(OUTPUT_DIR / d / "_summary.json")
        pages = load_json(OUTPUT_DIR / d / "_pages.json")
        result.append({
            "date": d,
            "total_pages": len(pages) if pages else 0,
            "total_articles": summary.get("total_articles", 0) if summary else 0,
        })
    return {"dates": result}


@app.get("/api/dates/{date_str}")
def api_date_detail(date_str: str):
    """取得指定日期嘅版面清單"""
    if date_str not in list_date_dirs():
        raise HTTPException(404, f"日期 {date_str} 唔存在")
    pages = load_json(OUTPUT_DIR / date_str / "_pages.json")
    summary = load_json(OUTPUT_DIR / date_str / "_summary.json")
    if not pages:
        raise HTTPException(404, "無版面資料")
    
    # Add image info for each page
    for p in pages:
        img_path = get_image_path(date_str, p["node_id"])
        p["has_image"] = img_path is not None
        if img_path:
            # Count articles
            art_dir = OUTPUT_DIR / date_str / "articles" / f"page_{p['node_id']}"
            art_count = len(list(art_dir.glob("*.json"))) if art_dir.exists() else 0
            p["article_count"] = art_count
        else:
            p["article_count"] = 0
    
    return {
        "date": date_str,
        "pages": pages,
        "summary": summary or {},
    }


@app.get("/api/dates/{date_str}/pages/{page_id}")
def api_page_detail(date_str: str, page_id: str):
    """取得指定版面的文章清單"""
    if date_str not in list_date_dirs():
        raise HTTPException(404, f"日期 {date_str} 唔存在")
    
    pages = load_json(OUTPUT_DIR / date_str / "_pages.json")
    page_info = None
    for p in pages or []:
        if p["node_id"] == page_id:
            page_info = p
            break
    
    if not page_info:
        raise HTTPException(404, f"版面 {page_id} 唔存在")
    
    art_dir = OUTPUT_DIR / date_str / "articles" / f"page_{page_id}"
    articles = []
    if art_dir.exists():
        for f in sorted(art_dir.glob("*.json")):
            data = load_json(f)
            if data:
                articles.append({
                    "id": data.get("id"),
                    "title": data.get("title", "無標題"),
                    "body_length": data.get("body_length", 0),
                    "url": data.get("url"),
                    "body_preview": (data.get("body_text", "") or "")[:200],
                })
    
    img_path = get_image_path(date_str, page_id)
    
    # Find adjacent pages
    prev_page_id = None
    next_page_id = None
    pages = load_json(OUTPUT_DIR / date_str / "_pages.json")
    if pages:
        for i, p in enumerate(pages):
            if p["node_id"] == page_id:
                if i > 0:
                    prev_page_id = pages[i - 1]["node_id"]
                if i < len(pages) - 1:
                    next_page_id = pages[i + 1]["node_id"]
                break
    
    return {
        "date": date_str,
        "page_id": page_id,
        "section": page_info.get("section", ""),
        "articles": articles,
        "has_image": img_path is not None,
        "prev_page_id": prev_page_id,
        "next_page_id": next_page_id,
    }


@app.get("/api/images/{date_str}/{page_id}.jpg")
def api_image(date_str: str, page_id: str):
    """Serve newspaper page image"""
    img_path = get_image_path(date_str, page_id)
    if not img_path:
        raise HTTPException(404, "圖片唔存在")
    return FileResponse(img_path, media_type="image/jpeg")


@app.get("/api/articles/{article_id}")
def api_article(article_id: str, date_str: str = Query(...), page_id: str = Query(...)):
    """取得完整文章內容"""
    if date_str not in list_date_dirs():
        raise HTTPException(404, f"日期 {date_str} 唔存在")
    
    art_dir = OUTPUT_DIR / date_str / "articles" / f"page_{page_id}"
    art_json = art_dir / f"{article_id}.json"
    art_html = art_dir / f"{article_id}.html"
    
    data = load_json(art_json)
    if not data:
        raise HTTPException(404, "文章唔存在")
    
    # Read full HTML content
    html_content = ""
    if art_html.exists():
        html_content = art_html.read_text("utf-8")
    
    return {
        "id": data.get("id"),
        "title": data.get("title", "無標題"),
        "section": data.get("section", ""),
        "date": data.get("date", date_str),
        "body_text": data.get("body_text", ""),
        "body_length": data.get("body_length", 0),
        "keywords": data.get("keywords", ""),
        "description": data.get("description", ""),
        "pub_info": data.get("pub_info", ""),
        "html_content": html_content,
    }


@app.get("/api/search")
def api_search(
    q: str = Query("", min_length=1),
    date_filter: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
):
    """關鍵字搜尋文章（支援分頁）"""
    if not q:
        return {"results": [], "total": 0, "page": 1, "page_size": 100, "total_pages": 0}
    
    q_lower = q.lower()
    results = []
    dates = list_date_dirs()
    
    if date_filter:
        if date_filter in dates:
            dates = [date_filter]
        else:
            return {"results": [], "total": 0, "page": 1, "page_size": page_size, "total_pages": 0}
    
    for d in dates:
        art_root = OUTPUT_DIR / d / "articles"
        if not art_root.exists():
            continue
        
        for page_dir in sorted(art_root.iterdir()):
            if not page_dir.is_dir():
                continue
            for f in sorted(page_dir.glob("*.json")):
                data = load_json(f)
                if not data:
                    continue
                title = data.get("title", "") or ""
                body = data.get("body_text", "") or ""
                keywords = data.get("keywords", "") or ""
                
                if (q_lower in title.lower() or 
                    q_lower in body.lower() or 
                    q_lower in keywords.lower()):
                    
                    # Find context around match
                    idx = body.lower().find(q_lower)
                    context = ""
                    if idx >= 0:
                        start = max(0, idx - 80)
                        end = min(len(body), idx + len(q) + 80)
                        context = body[start:end]
                    
                    results.append({
                        "article_id": data.get("id"),
                        "title": title,
                        "date": d,
                        "page_id": data.get("page", ""),
                        "section": data.get("section", ""),
                        "context": context,
                        "body_length": data.get("body_length", 0),
                    })
    
    # Sort by date (newest first), then by title
    results.sort(key=lambda r: (r["date"], r["title"] or ""), reverse=True)
    
    total = len(results)
    total_pages = max(1, (total + page_size - 1) // page_size)
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    
    return {
        "results": results[start_idx:end_idx],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
    }


# ── Frontend ──
STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
@app.get("/modaily", include_in_schema=False)
async def root():
    return RedirectResponse(url="/modaily/")


@app.get("/modaily/{rest:path}", include_in_schema=False)
async def serve_frontend(rest: str):
    """Serve the SPA frontend. Any path under /modaily/ returns index.html (SPA)."""
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(404, "Frontend not found")
    
    # /modaily/m → page viewer (short URL, kept throughout navigation)
    if rest == "m":
        viewer_path = STATIC_DIR / "page-viewer.html"
        if viewer_path.exists():
            return FileResponse(viewer_path)
    
    # Serve actual static files if they exist
    static_file = STATIC_DIR / rest
    if rest and static_file.exists() and static_file.is_file():
        return FileResponse(static_file)
    
    return HTMLResponse(index_path.read_text("utf-8"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=API_PORT)
