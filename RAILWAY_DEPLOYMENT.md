# Railway Deployment Guide

Deploy your Corgi Copy Trading Bot to Railway.app with persistent storage and automatic restarts.

## Prerequisites

- GitHub repository with this code
- Railway.app account (free tier works for testing)
- Hyperliquid API wallet created (NOT your main wallet!)
- Portal credentials (email/password)

## Quick Deploy

### 1. Create Railway Project

1. Go to [railway.app](https://railway.app) and sign in
2. Click **"New Project"** → **"Deploy from GitHub repo"**
3. Select your repository
4. Railway will automatically detect the configuration from `railway.toml`

### 2. Create Persistent Volume

**CRITICAL:** Without a volume, your database resets on every deployment!

1. In your Railway project, click **"+ New"** → **"Volume"**
2. Name it `corgi-data`
3. Set mount path to `/data`
4. Click **"Add Volume"**

### 3. Configure Environment Variables

In the Railway dashboard, go to **Variables** and set:

#### Required Variables

```bash
# Portal Credentials
PORTAL_USER=your@email.com
PORTAL_PASSWORD=yourpassword

# Hyperliquid Credentials
HL_WALLET_ADDRESS=0x<your_main_wallet_address>
HL_PRIVATE_KEY=0x<api_sub_wallet_private_key>

# Database Path (uses the volume mount)
RAILWAY_VOLUME_MOUNT_PATH=/data
CORGI_DB_PATH=${RAILWAY_VOLUME_MOUNT_PATH}/corgi.db

# Safety - START WITH DRY_RUN=true!
DRY_RUN=true
```

#### Optional Variables (defaults are in railway.toml)

```bash
# Trading Config
HL_LEVERAGE=10
HL_MARGIN_USD=100
HL_MARGIN_MODE=isolated

# Caller Whitelist
ALLOWED_CALLERS=voberoi,pranayyyy,corgil_

# HIP-3 Dex Priority
HL_DEX_PRIORITY=xyz,cash,flx

# Reconciliation
RECONCILE_INTERVAL_SECONDS=60
HL_CHANGE_DEBOUNCE_SECONDS=2.0

# Logging
LOG_LEVEL=INFO

# Notifications (optional)
NOTIFY_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

### 4. Deploy

Railway will automatically deploy when you push to your repo's main branch.

**First Deployment:**
1. Check logs: Railway dashboard → **Deployments** → **View Logs**
2. Look for: `Bot ready — dashboard at http://0.0.0.0:8080`
3. Access dashboard: Your Railway project has a public URL (click **"Settings"** → **"Generate Domain"**)

## Accessing the Dashboard

Railway provides a public URL for your bot's NiceGUI dashboard:

1. Go to **Settings** → **Networking** → **Generate Domain**
2. Railway will give you a URL like `https://your-bot.railway.app`
3. Visit this URL to see the live dashboard with trade cards, stats, and controls

**Security Note:** The dashboard is publicly accessible. Consider adding authentication if needed.

## Health Checks

Railway automatically monitors your bot's health:
- Health check endpoint: `/` (the dashboard homepage)
- Timeout: 300 seconds
- The bot is considered healthy if the NiceGUI server responds

## Graceful Shutdown

Railway sends SIGTERM before stopping your bot, giving it 10 seconds to:
- Close open database connections
- Stop the portal poll loop
- Close HL WebSocket connections
- Flush logs

The bot's `on_shutdown` handlers in `app/main.py` handle this automatically.

## Auto-Restart Policy

Railway will automatically restart your bot if it crashes:
- **Policy:** `ON_FAILURE` (only restart on crash, not on clean exit)
- **Max Retries:** 10 consecutive failures
- After 10 failures, Railway stops trying and alerts you

This is configured in `railway.toml`.

## Monitoring & Logs

### View Logs

Railway dashboard → **Deployments** → **View Logs**

Look for:
```
INFO    Bot ready — dashboard at http://0.0.0.0:8080
INFO    portal poll supervisor active
INFO    HL price feed connected
INFO    routing event: new_trade trade #XXX
```

### Common Issues

**Database resets on deploy:**
- ✅ Check that the volume is mounted to `/data`
- ✅ Verify `CORGI_DB_PATH=${RAILWAY_VOLUME_MOUNT_PATH}/corgi.db` is set
- ✅ Check Railway logs for "Database initialized at /data/corgi.db"

**Portal auth errors:**
- ✅ Verify `PORTAL_USER` and `PORTAL_PASSWORD` are correct
- ✅ Check Railway logs for "portal login successful"

**HL connection errors:**
- ✅ Verify `HL_WALLET_ADDRESS` is your MAIN wallet (holds funds)
- ✅ Verify `HL_PRIVATE_KEY` is the API sub-wallet key (NOT main wallet!)
- ✅ Check Railway logs for "HL wrapper ready"

**Dashboard not accessible:**
- ✅ Generate a domain in Railway Settings → Networking
- ✅ Check Railway logs for "Bot ready — dashboard at..."
- ✅ Verify port 8080 is exposed (Railway auto-detects this)

## Production Checklist

Before going live with real trades:

- [ ] Volume mounted and database persisting
- [ ] `DRY_RUN=true` tested successfully (bot shows STALE cards for old trades)
- [ ] Fresh test trade appears on dashboard and logs show:
  - `routing event: new_trade trade #XXX`
  - `handle_new_trade: #XXX <coin> <side> caller=<caller>`
- [ ] Set `DRY_RUN=false` (bot will place REAL orders!)
- [ ] Verify `HL_LEVERAGE` and `HL_MARGIN_USD` are appropriate
- [ ] Set `ALLOWED_CALLERS` to only trusted traders
- [ ] Configure `NOTIFY_WEBHOOK_URL` for alerts (Discord/Telegram)
- [ ] Monitor first real trade closely:
  - Check Railway logs for `OPEN <coin> #XXX`
  - Verify position appears on HL mainnet
  - Verify entry in database (`hl_live_trades`)

## Updating the Bot

Railway auto-deploys when you push to GitHub:

```bash
git add .
git commit -m "Update bot configuration"
git push origin main
```

Railway will:
1. Pull the latest code
2. Rebuild (if dependencies changed)
3. Send SIGTERM to the old process (graceful shutdown)
4. Start the new process
5. The database persists through the update (stored on volume)

## Cost Estimate

**Free Tier (Hobby Plan):**
- ✅ Enough for testing and low-volume trading
- 500 hours/month execution time
- $5 credit/month

**Pro Plan ($20/month):**
- ✅ Recommended for production
- Unlimited execution time
- Better uptime SLA
- Priority support

**Volume Storage:**
- Free: 1GB
- Enough for the SQLite database (typically <100MB)

## Backup Strategy

Your data lives in the Railway volume. To back up:

1. Use Railway CLI to download the database:
   ```bash
   railway run sqlite3 /data/corgi.db .dump > backup.sql
   ```

2. Or export via dashboard API calls

3. For critical production use, consider:
   - Periodic database snapshots
   - Secondary backup to S3/GCS
   - Transaction log archival

## Troubleshooting

### Bot keeps restarting

Check Railway logs for errors. Common causes:
- Missing environment variables
- Invalid credentials
- Port binding issues (Railway should auto-detect port 8080)

### Dashboard shows old trades as STALE

✅ **This is correct!** Trades opened before the bot started are marked STALE and show as dashboard cards only (not auto-entered).

### Events not processing

Check Railway logs for:
- `routing event:` lines appearing (proves events are being processed)
- `handle_new_trade:` lines appearing (proves handlers are executing)
- Any error messages

### Database connection errors

- Verify `CORGI_DB_PATH` points to the volume mount
- Check Railway logs for "Database initialized at /data/corgi.db"
- Ensure the volume is attached and healthy

## Security Notes

1. **Never commit `.env` to Git** — it contains your private keys!
2. **Use Railway's environment variables** for all secrets
3. **Start with `DRY_RUN=true`** until thoroughly tested
4. **Use an API sub-wallet** for `HL_PRIVATE_KEY`, NOT your main wallet
5. **Whitelist only trusted callers** in `ALLOWED_CALLERS`
6. **Monitor the first few real trades** closely

## Support

- Railway Docs: https://docs.railway.app
- Bot Issues: https://github.com/<your-repo>/issues
- Hyperliquid API: https://hyperliquid.gitbook.io/

---

**Ready to deploy!** 🚀

Start with `DRY_RUN=true`, verify everything works, then flip to `DRY_RUN=false` when you're confident.
