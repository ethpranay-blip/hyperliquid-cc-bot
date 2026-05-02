# Deployment

## Running locally

### Prerequisites
- Python **3.9+** (project tested with 3.9 system Python on macOS — `/Library/Developer/CommandLineTools/usr/bin/python3`)
- Outbound HTTPS to `portal.corgicalls.com` and `api.hyperliquid.xyz` (+ `/ws`)
- ~200 MB disk for SQLite + logs

### First-time setup

```bash
git clone <this-repo>
cd <repo>

# Python deps
pip install -r requirements.txt

# Config template
cp env.example .env
# → edit .env with your real credentials (see env-var reference below)

# Launch
python -m app.main
```

Dashboard at **http://localhost:8080**.

### Day-to-day

```bash
# Launch (foreground)
python -m app.main

# Launch (detached, write logs to /tmp)
python -m app.main > /tmp/corgi-bot.log 2>&1 &
echo "PID: $!"

# Check it's alive
lsof -i :8080
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://localhost:8080/

# Tail live logs
tail -f app.log

# Stop
kill <PID>      # or: pkill -f "python.*app.main"
```

### Clean restart (wipe SQLite state)

```bash
kill $(lsof -ti :8080) 2>/dev/null
mv data/corgi.db data/corgi-archived-$(date +%F).db
# WAL sidecars get recreated; if stale, remove them:
rm -f data/corgi.db-shm data/corgi.db-wal
python -m app.main
```

Schema migrations (e.g. the `my_fill_price` column) run automatically on next launch via `db._apply_migrations()`.

---

## Environment variables

### Required

| Var | Description | Example |
|---|---|---|
| `PORTAL_USER` | Corgi Portal username (NOT email — the handle you log in with) | `pranay` |
| `PORTAL_PASSWORD` | Corgi Portal password | `hunter2` |
| `HL_WALLET_ADDRESS` | Your main wallet address (holds the USDC) | `0xEb93…2696` |
| `HL_PRIVATE_KEY` | **API sub-wallet** private key (not your main wallet key!) | `0x…` |

### Trading config

| Var | Default | Description |
|---|---|---|
| `HL_LEVERAGE` | `10` | Default leverage when the portal event doesn't specify one. Capped per-asset at HL's `maxLeverage`. |
| `HL_MARGIN_USD` | `100` | USDC margin per trade. Notional = `HL_MARGIN_USD × leverage`. Minimum $10 notional enforced. |
| `ALLOWED_CALLERS` | `voberoi,pranayyyy,corgil_` | Comma-separated userTags to copy. Trades from others silently ignored (logged once per trade ID). |

### Network / safety

| Var | Default | Description |
|---|---|---|
| `HL_TESTNET` | `false` | `true` → use `api.hyperliquid-testnet.xyz` (separate account universe — test keys are different from mainnet keys) |
| `DRY_RUN` | `true` | `true` → log what would happen, return mock responses, place **no real orders** |
| `AUTO_MODE` | `false` | `true` → bot auto-enters every fresh whitelisted `new_trade`. Loud warning logged at startup if combined with `DRY_RUN=false`. |

### Observability

| Var | Default | Description |
|---|---|---|
| `LOG_LEVEL` | `INFO` | Standard Python levels: DEBUG / INFO / WARNING / ERROR |
| `NOTIFY_WEBHOOK_URL` | *(empty)* | Discord webhook or Telegram bot URL. Empty = no notifications. Auto-detects shape. |

### Advanced (rarely needed)

| Var | Default | Description |
|---|---|---|
| `PORTAL_BASE_URL` | `https://portal.corgicalls.com` | Override portal base (if they ever relocate) |
| `PORTAL_POLL_INTERVAL` | `3.0` | Seconds between activity-feed polls |
| `HL_BASE_URL` | (network-dependent) | Override HL REST base |
| `HL_WS_URL` | (network-dependent) | Override HL WS base |
| `CORGI_DB_PATH` | `./data/corgi.db` | SQLite file path |
| `PORT` | `8080` | Dashboard HTTP port |
| `HOST` | `0.0.0.0` | Dashboard bind address |

---

## HL credential setup

**Critical:** `HL_PRIVATE_KEY` must be an **API sub-wallet** key, never your main wallet's key.

### Mainnet
1. Go to https://app.hyperliquid.xyz
2. Connect your main wallet
3. Navigate to **API**
4. **Create API Wallet** — give it a name (e.g. "Copytrading")
5. Copy the generated private key and paste it in `.env` as `HL_PRIVATE_KEY`
6. Your main wallet address goes in `HL_WALLET_ADDRESS`

### Testnet
Same flow but on https://app.hyperliquid-testnet.xyz — testnet and mainnet API sub-wallets are **separate universes**, a testnet key won't work against mainnet and vice versa.

### Verify registration
```bash
python3 -c "
import httpx, os
for ln in open('.env'):
    if '=' in ln and not ln.strip().startswith('#'):
        k,_,v = ln.strip().partition('='); os.environ.setdefault(k, v)
from eth_account import Account
sub = Account.from_key(os.environ['HL_PRIVATE_KEY']).address
r = httpx.post('https://api.hyperliquid.xyz/info',
               json={'type':'extraAgents','user':os.environ['HL_WALLET_ADDRESS']},
               timeout=15).json()
addrs = [a['address'].lower() for a in r]
print(f'derived sub-wallet: {sub}')
print(f'registered? {sub.lower() in addrs}')
print(f'agents: {r}')
"
```

---

## Safety progression (recommended first launch)

1. **`DRY_RUN=true`** + mainnet creds → verify portal events parse, dashboard renders. No real orders.
2. **`HL_TESTNET=true`** + testnet keys + `DRY_RUN=false` + small `HL_MARGIN_USD` (25) → verify order placement, bracket execution, SL updates, partial TPs. Testnet USDC is free.
3. **`HL_TESTNET=false`** + mainnet keys + `DRY_RUN=false` + small `HL_MARGIN_USD` (25-50) + **`AUTO_MODE=false`** → first mainnet fire, manually click Enter on one card.
4. **`AUTO_MODE=true`** → full automation. Scale margin up once confident.

---

## Moving to a VPS

### Sizing
- **Minimum**: 1 vCPU, 1 GB RAM, 10 GB disk (e.g. DigitalOcean $6/mo droplet, Hetzner CPX11)
- Low latency to HL's infra is nice but not critical — the bot places IOC orders with 5% slippage cushion, so 100-200ms round-trip is fine

### Quick setup on Ubuntu 22.04

```bash
# System deps
sudo apt update && sudo apt install -y python3 python3-pip python3-venv git

# User + directory
sudo useradd -m -s /bin/bash corgi
sudo -u corgi -i

# Clone + install
git clone <your-repo> ~/corgi-bot
cd ~/corgi-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Config
cp env.example .env
chmod 600 .env                # private-key file — keep it locked
nano .env                     # fill in real values
```

### systemd unit (recommended)

`/etc/systemd/system/corgi-bot.service`:

```ini
[Unit]
Description=Corgi Calls Copy Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=corgi
WorkingDirectory=/home/corgi/corgi-bot
# Load .env into the environment
EnvironmentFile=/home/corgi/corgi-bot/.env
ExecStart=/home/corgi/corgi-bot/venv/bin/python -m app.main
Restart=on-failure
RestartSec=10
# Logs go to journalctl
StandardOutput=journal
StandardError=journal

# Hardening (optional but recommended)
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/corgi/corgi-bot

[Install]
WantedBy=multi-user.target
```

Activate:
```bash
sudo systemctl daemon-reload
sudo systemctl enable corgi-bot
sudo systemctl start corgi-bot
sudo systemctl status corgi-bot
journalctl -u corgi-bot -f               # live logs
```

> **`EnvironmentFile` caveat**: systemd expects plain `KEY=VALUE` pairs with no inline `# comments` on the same line. Strip comments from `.env` before pointing systemd at it, or load env inside the Python process via `load_dotenv()` and omit `EnvironmentFile` (this is already the current default since `main.py` calls `load_dotenv()` on import).

### Dashboard access over the network

The dashboard binds to `0.0.0.0:8080` by default. Options:

**A — SSH tunnel (safest, no auth layer needed)**

```bash
ssh -L 8080:localhost:8080 corgi@<vps-ip>
# now open http://localhost:8080 on your laptop
```

**B — Caddy reverse proxy with basic auth** (if you want it reachable over the internet)

```caddyfile
corgi.example.com {
    basicauth {
        admin JDJhJDE0JDBOY...   # bcrypt hash from: caddy hash-password
    }
    reverse_proxy localhost:8080
}
```

`sudo apt install caddy` — auto-provisions TLS via Let's Encrypt.

> The bot has NO built-in authentication. Exposing `:8080` directly to the internet means anyone who finds it can click the red "Cancel" button or manually enter trades. Always tunnel, VPN, or put behind a reverse-proxy with auth.

### Backup

```bash
# Cron daily SQLite snapshot
0 3 * * * /usr/bin/sqlite3 /home/corgi/corgi-bot/data/corgi.db ".backup /home/corgi/backups/corgi-$(date +\%F).db"
```

Rotate old backups with `find /home/corgi/backups/ -mtime +30 -delete`.

### Log rotation

The bot already rotates `app.log` (5 MB × 3 backups) via `logging.handlers.RotatingFileHandler`. If you prefer system logrotate:

`/etc/logrotate.d/corgi-bot`:
```
/home/corgi/corgi-bot/app.log* {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
```

### Updates

```bash
sudo systemctl stop corgi-bot
cd /home/corgi/corgi-bot
sudo -u corgi git pull
sudo -u corgi venv/bin/pip install -r requirements.txt
sudo systemctl start corgi-bot
sudo systemctl status corgi-bot
```

Schema changes handled by `_apply_migrations()` at startup — no manual `ALTER TABLE` needed.

### Docker alternative

If you prefer containers, a minimal `Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ ./app/
EXPOSE 8080
CMD ["python", "-m", "app.main"]
```

```bash
docker build -t corgi-bot .
docker run -d --name corgi-bot --env-file .env \
  -v corgi-data:/app/data \
  -p 127.0.0.1:8080:8080 \
  --restart unless-stopped \
  corgi-bot
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Portal login returns 401 `Invalid password` | Wrong endpoint path or field. Should be `POST /api/portal/login` with `username` (not `email`). |
| `User or API Wallet 0x… does not exist` | Sub-wallet key not registered on this network. Generate one at app.hyperliquid.xyz → API (or testnet equivalent) and re-paste into `.env`. |
| `Insufficient margin to place order` | No USDC in Perps (or Spot on a unified account). Deposit or transfer. |
| `asset 'XXX' not found on any dex` | Asset not listed on HL testnet (mainnet has more). Normal rejection, bot moves on. |
| `Trading is halted` | HL paused that market. Server-side state, not a bug. |
| `Cannot modify canceled or filled order` | You're on an old build — update `update_stop` to the cancel+replace pattern. |
| `Unexpected number of trigger orders` | You're on an old build — update `open_trade` to use `grouping="na"` when SL is None. |
| Dashboard HTTP 500 with `sanitize` error | NiceGUI version mismatch — pass `sanitize=False` to `ui.html()`. |
| "Replaying every trade from backlog on restart" | You're on an old build — need startup-time cutoff + `seed_closed_from_backlog` + `get_closed_trade` dedup check. |
