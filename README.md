# Corgi Calls Copy Trading Bot

Automated copy trading bot that mirrors trade signals from the [Corgi Calls Portal](https://portal.corgicalls.com) to [Hyperliquid](https://app.hyperliquid.xyz) perpetual futures.

## Features

- Real-time portal monitoring with ~3s polling
- Automatic or manual trade entry via NiceGUI dashboard
- Stop-loss updates, partial take-profits, full closes
- Live price feed via Hyperliquid WebSocket
- Caller whitelist filtering (voberoi, pranayyyy, corgil_)
- DRY_RUN mode for safe testing
- SQLite persistence — survives restarts
- Discord/Telegram webhook notifications
- k-coin and HIP-3 coin support

## Quick Start

```bash
# 1. Clone and enter the project
cd corgicalls-bot

# 2. Create virtual environment
python3.12 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up credentials
cp .env.example .env
# Edit .env with your portal login, HL wallet, and API sub-wallet key

# 5. Run (starts dashboard at localhost:8080)
python -m app.main
```

## First Run Checklist

1. **Set `DRY_RUN=true`** in `.env` — verify portal connection and event parsing
2. **Generate an API sub-wallet** at [app.hyperliquid.xyz](https://app.hyperliquid.xyz) → API
3. **Never use your main wallet private key** — always the API sub-wallet key
4. **Start with small `HL_MARGIN_USD`** when going live (e.g., $25-50)
5. **Monitor the dashboard** at `http://localhost:8080` during first live session

## Project Structure

```
corgicalls-bot/
├── app/
│   ├── __init__.py
│   ├── main.py                # NiceGUI dashboard + event loops
│   ├── portal.py              # Portal API client (auth, polling, parsing)
│   ├── hyperliquid_client.py  # HL SDK wrapper (orders, WS prices)
│   ├── db.py                  # SQLite persistence layer
│   └── notifier.py            # Webhook notifications
├── .env.example               # Credential template
├── .gitignore
├── requirements.txt
├── GAMEPLAN.md                # Full build spec and architecture
└── README.md
```

## Dashboard

Access at `http://localhost:8080` after starting the bot.

- **Active Trade Cards** — live positions with real-time PnL, Enter/Cancel buttons
- **Historic Trades Table** — closed positions with entry, exit, PnL
- **Activity Feed** — real-time portal events sidebar
- **Auto Mode** — toggle automatic trade entry (per-session, defaults OFF)
- **Stats Header** — total realized PnL, win rate, open positions count

## Safety

- All credentials stored in `.env` (gitignored)
- API sub-wallet limits exposure vs main wallet
- DRY_RUN mode logs actions without placing orders
- BLOCKED guard prevents double-entry on same coin
- Deduplication prevents processing the same trade twice
