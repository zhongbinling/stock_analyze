# Deployment Guide (English)

> For v1.0.0 (Phase 1 complete). 中文版: [../部署手册.md](../部署手册.md)
> Unified entry points on all three platforms: `scripts/service.*` (service management) and `scripts/deploy*` (one-command deployment).

## 1. Requirements

- Python 3.10+ (3.12 / 3.14 verified)
- Network access to GitHub (clone/upgrade) and the CN market-data endpoints (EastMoney / THS / Tencent)
- < 500 MB disk including dependencies and data

## 2. One-command deploy

**Windows**

```bat
git clone https://github.com/zhongbinling/stock_analyze.git
cd stock_analyze
scripts\deploy_win.bat
```

**macOS / Linux**

```bash
git clone https://github.com/zhongbinling/stock_analyze.git
cd stock_analyze
./scripts/deploy_mac.sh        # or ./scripts/deploy_linux.sh / ./scripts/deploy.sh
```

The script: checks Python → creates `.venv` → installs dependencies → runs DB migrations → **interactively creates the super-admin if no account exists** (skipped otherwise) → optionally registers auto-start → starts the service → health-checks and prints the URL.

## 3. Service management (identical commands on all platforms)

| Action | Windows | macOS / Linux |
|---|---|---|
| start | `scripts\service.bat start` | `./scripts/service.sh start` |
| stop | `scripts\service.bat stop` | `./scripts/service.sh stop` |
| status | `scripts\service.bat status` | `./scripts/service.sh status` |
| restart | `scripts\service.bat restart` | `./scripts/service.sh restart` |
| enable auto-start | `… install` | `… install` |
| disable auto-start | `… uninstall` | `… uninstall` |

- `start` runs an idempotent DB migration first, then launches in the background; a running instance is detected via `/healthz` and not double-started
- Runtime log: `data/service.log`; PID file: `data/service.pid`
- Auto-start: Windows = HKCU registry Run key (on logon, user-level, no admin rights), Linux = systemd unit (sudo), macOS = launchd (user-level)
- Scripts prefer the `.venv` interpreter and fall back to system Python

## 4. Post-deploy setup

1. Sign in to the admin console: `http://127.0.0.1:8501/admin/login` (super-admin created during deploy)
2. First data backfill (~8–12 min): Admin → **Data Management** → *Update now* (or `cd core && python fetch_daily.py`)
3. Generate & publish the sentiment view: Admin → **Data Generation** → *Generate* on the prod rule
   (afterwards it auto-generates and publishes at 17:00 every trading day)
4. User access: `http://127.0.0.1:8501/mkt` (no login required)

## 5. Manual deploy

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt   # Win: .venv\Scripts\...
export FLASK_APP="server:create_app()"
python -m flask db upgrade
python -m server.bootstrap --username admin
python -m server
```

## 6. Upgrade / 7. Backup

- `git pull` → `service.* restart` (start re-runs migrations). If `requirements.txt` changed, reinstall then restart.
- Backup (service stopped): `data/app.db` (accounts/rules/jobs), `core/data/sentiment.db` (market data), `config/` (settings). Views under `views_store/` are regenerable.

## 8. Troubleshooting

- **Port busy** — change `port` in `config/server.yaml` and restart; find strays with `netstat -ano | findstr 8501` (Linux: `lsof -i:8501`)
- **LAN access** — set `host: "0.0.0.0"` and open the firewall port; **put an HTTPS reverse proxy in front** (the service itself has no TLS / internet hardening)
- **Lost admin password** — `python -m server.bootstrap --username admin2 --force`, then delete the old account from Account Management
- **Migration errors** — run `python -m flask --app "server:create_app()" db upgrade` alone; check `db current`
- **Data update failures** — usually upstream rate-limiting; retry later; auto-falls back EastMoney→Tencent for index klines
- **Won't start** — tail `data/service.log`; usually missing deps (re-run deploy) or port in use
