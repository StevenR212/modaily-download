# 澳門日報 — API Reference

Base URL: `http://<server>:5678`

**Note:** When deployed behind nginx, all endpoints are accessible at `/api/...` under the root domain (e.g. `http://server-ip/api/dates`).

---

## `GET /api/dates`

List all available dates.

**Response:**
```json
{
  "dates": [
    {"date": "2026-06-29", "total_pages": 34, "total_articles": 210},
    {"date": "2026-06-28", "total_pages": 30, "total_articles": 192},
    ...
  ]
}
```

---

## `GET /api/dates/{date_str}`

Get all pages for a specific date.

**Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `date_str` | string | Date in `YYYY-MM-DD` format |

**Response:**
```json
{
  "date": "2026-06-29",
  "pages": [
    {
      "node_id": "A01",
      "node_file": "node_A01.html",
      "section": "澳聞",
      "has_image": true,
      "article_count": 5
    },
    ...
  ],
  "summary": {
    "newspaper": "澳門日報 (Macau Daily News)",
    "date": "2026-06-29",
    "total_pages": 34
  }
}
```

---

## `GET /api/dates/{date_str}/pages/{page_id}`

Get article list for a specific page.

**Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `date_str` | string | Date in `YYYY-MM-DD` |
| `page_id` | string | Page identifier (e.g. `A01`, `B03`) |

**Response:**
```json
{
  "date": "2026-06-29",
  "page_id": "A01",
  "section": "澳聞",
  "articles": [
    {
      "id": "488042",
      "title": "旅遊合作助建亞太共同體",
      "body_length": 1192,
      "url": "https://...content_488042.html",
      "body_preview": "（澳門日報消息）..."
    }
  ],
  "has_image": true,
  "prev_page_id": null,
  "next_page_id": "A02"
}
```

---

## `GET /api/images/{date_str}/{page_id}.jpg`

Serve a newspaper page scan image.

**Parameters:**
| Param | Type | Description |
|-------|------|-------------|
| `date_str` | string | Date in `YYYY-MM-DD` |
| `page_id` | string | Page identifier (e.g. `A01`) |

**Response:** JPEG image (Content-Type: `image/jpeg`)

---

## `GET /api/articles/{article_id}`

Get full article content.

**Query Parameters:**
| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `date_str` | string | yes | Date in `YYYY-MM-DD` |
| `page_id` | string | yes | Page identifier (e.g. `A01`) |

**Response:**
```json
{
  "id": "488042",
  "title": "旅遊合作助建亞太共同體",
  "section": "澳聞",
  "date": "2026-06-29",
  "body_text": "（澳門日報消息）...",
  "body_length": 1192,
  "keywords": "旅遊,亞太,合作",
  "description": "旅遊合作助建亞太共同體...",
  "pub_info": "2026-06-29 06:30:00",
  "html_content": "<founder-content><!--enpcontent--><p>全文HTML</p>..."
}
```

---

## `GET /api/search`

Full-text search across all scraped articles.

**Query Parameters:**
| Param | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `q` | string | yes | — | Search keyword (case-insensitive) |
| `date_filter` | string | no | — | Restrict to one date (`YYYY-MM-DD`) |
| `page` | integer | no | `1` | Page number (1-indexed) |
| `page_size` | integer | no | `100` | Results per page (max 500) |

**Response:**
```json
{
  "results": [
    {
      "article_id": "488042",
      "title": "旅遊合作助建亞太共同體",
      "date": "2026-06-29",
      "page_id": "A01",
      "section": "澳聞",
      "context": "...全文涉及旅遊合作...",
      "body_length": 1192
    }
  ],
  "total": 47,
  "page": 1,
  "page_size": 100,
  "total_pages": 1
}
```

---

## Frontend Routes

| Route | Description |
|-------|-------------|
| `GET /` | Redirect → `/modaily/` |
| `GET /modaily` | Redirect → `/modaily/` |
| `GET /modaily/` | Main SPA (grid/list/flip views, date sidebar, search, article reader) |
| `GET /modaily/m` | Mobile-optimised page viewer |
| `GET /modaily/{path}` | Serve static files from `static/` dir, fallback to SPA |

## Error Responses

All API endpoints return standard HTTP codes:

| Code | Meaning |
|------|---------|
| 200 | Success |
| 404 | Resource not found (date, page, article, image) |
| 422 | Invalid query parameters (validation error) |
