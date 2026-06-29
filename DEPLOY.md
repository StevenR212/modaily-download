# 澳門日報 — Deployment Guide

## Overview

Deployed on a **NAS VM** (Ubuntu 24.04, 25GB RAM, 97GB LVM) via:
- **Systemd** — auto-start + restart FastAPI server
- **Nginx** — reverse proxy `/modaily/` at port 80
- **OS Cron** — daily scraping at 07:00 CST
- **Hermes Agent** — optional management via Telegram

## Server Setup

### 1. Install Dependencies

```bash
apt update && apt install -y python3 python3-pip nginx
pip3 install fastapi uvicorn requests
```

### 2. Deploy Code

```bash
mkdir -p /root/modaily_server/static
# Copy main.py to /root/modaily_server/
# Copy static/index.html and static/page-viewer.html to /root/modaily_server/static/
```

### 3. Create Output Directory

```bash
mkdir -p /root/modaily_output
```

### 4. Systemd Service

Create `/etc/systemd/system/modaily.service`:

```ini
[Unit]
Description=澳門日報 FastAPI Browser
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/modaily_server
Environment=MODAILY_OUTPUT=/root/modaily_output
Environment=MODAILY_PORT=5678
ExecStart=/usr/bin/python3 /root/modaily_server/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:

```bash
systemctl daemon-reload
systemctl enable --now modaily
systemctl status modaily
```

### 5. Nginx Reverse Proxy

Create `/etc/nginx/sites-available/modaily`:

```nginx
server {
    listen 80;
    server_name _;

    # 澳門日報瀏覽器 — proxy to FastAPI
    location /modaily/ {
        proxy_pass http://127.0.0.1:5678/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }

    # API endpoints
    location /api/ {
        proxy_pass http://127.0.0.1:5678/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 60s;
    }

    # Redirect /modaily -> /modaily/
    location = /modaily {
        return 301 /modaily/;
    }
}
```

Activate:

```bash
ln -sf /etc/nginx/sites-available/modaily /etc/nginx/sites-enabled/
nginx -t && systemctl reload nginx
```

### 6. Cron Job — Daily Scraping

Add to root's crontab (`crontab -e`):

```cron
0 7 * * * cd /root && /usr/bin/python3 /root/crawl_modaily.py --output /root/modaily_output >> /var/log/modaily_cron.log 2>&1
```

Use `crawl_modaily_quiet.py` for quieter logs.

## Configuration

Environment variables (for systemd `[Service]` section):

| Variable | Default | Description |
|----------|---------|-------------|
| `MODAILY_OUTPUT` | `/root/modaily_output` | Path to scraped newspaper data |
| `MODAILY_PORT` | `5678` | FastAPI listening port |

## Verification

```bash
# Check service is running
curl -s http://localhost:5678/modaily/ | head -5

# Check API
curl -s http://localhost:5678/api/dates | python3 -m json.tool | head -20

# Check nginx proxy
curl -s http://localhost/modaily/ | head -5

# Check search
curl -s "http://localhost:5678/api/search?q=澳門" | python3 -c "import sys,json; d=json.load(sys.stdin); print(f'Total: {d[\"total\"]}')"
```

## Troubleshooting

| Problem | Check |
|---------|-------|
| Server won't start | `journalctl -u modaily --no-pager -n 50` |
| Nginx 502 Bad Gateway | Is `modaily.service` running? Check port 5678 |
| Scraper not running | `tail -20 /var/log/modaily_cron.log` |
| Missing dates | Check `/root/modaily_output/` directory listing |
| Disk full | `du -sh /root/modaily_output/` |

## Migration to New Server

```bash
# Stop services on old server
systemctl stop modaily

# Copy code
rsync -avz /root/modaily_server/ user@new:/root/modaily_server/

# Copy data (large — may take hours)
rsync -avz --progress /root/modaily_output/ user@new:/root/modaily_output/

# Set up systemd, nginx, cron on new server (steps 4-6 above)
# Point DNS to new IP
```
