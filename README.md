# 🚀 Gem Alert Bot

**Solana memecoin gem spotter** — scans DexScreener, verifies with RugCheck, alerts you via Telegram when a token shows **2x potential**.

Built by [@thomas_young](https://github.com/Thomasyoung113)

---

## ✨ Features

### Core Scanner
- **Filter Pipeline** — MCap $200k–$1M, age <72h, buys≥50, sells≥30, txs(5m)≥10, vol(5m)≥$10k, liq≥$8.5k
- **RugCheck Safety** — LP locked 100%, risk score check, mint/freeze authority verification
- **Telegram Alerts** — Rich HTML alerts with MCap, volume, price, target 2x, smart wallet tags
- **`/start` Welcome** — Bot responds with feature overview (chat ID whitelist enforced)

### Outcome Tracker
- Monitors alerted tokens every 5min for 2x target
- Records success/failure, peak MCap, time to target
- Displays live success rate on every alert

### Self-Learning Engine
- Analyzes historical outcomes to find optimal filter ranges
- Automatically tightens MCap range, volume thresholds, buy/sell counts
- Activates after 10 resolved calls — improves over time

### Phase 2 — Smart Money Detection
- **Wide Scanner** — Monitors Solana tokens hitting $10M+ MCap
- **Early Buyer Profiling** — Captures first 100 buyers via Solana RPC
- **Wallet Scoring** — Tracks wallet hit rates, flags smart money (>60% success, ≥3 picks)
- **Alert Tagging** — Shows ✅ smart wallet activity on new alerts

### Security
- **Insider Sell Detection** — Monitors top holders for dumps >10%
- **Whale Wallet Alerts** — Tracks known whale wallets for new token buys
- **Dashboard Auth** — Bearer token protection on `/api/*` endpoints
- **HTML Sanitization** — `html.escape()` on all user-controlled data
- **Chat Whitelist** — Only responds to authorized chat ID
- **`.env` Lockdown** — `chmod 600` on sensitive files

### Web Dashboard
- Live stats, recent alerts, color-coded log viewer
- Polls every 5s via JavaScript `fetch()`
- Dark theme, runs on `http://127.0.0.1:8080`

### Backtesting Engine
- Run filter pipeline against historical DexScreener data
- Reports optimal MCap range, volume thresholds, rejection breakdown
- Results stored in SQLite for later analysis

---

## 🚦 Quick Start

```bash
cd ~/alert-bot
cp .env.example .env
# Edit .env with your Telegram bot token and chat ID

pip install -r requirements.txt
./run.sh start        # Start bot
./run.sh status       # Check status
./run.sh stop         # Stop bot
tail -f bot.log       # Live logs
```

### Run backtest
```bash
python3 -c 'from bot.backtest import run_backtest; run_backtest()'
```

### Web dashboard
Open `http://127.0.0.1:8080` in your browser.

---

## 📁 Project Structure

```
alert-bot/
├── main.py              # Entry point
├── config.py            # Settings
├── requirements.txt     # Dependencies
├── run.sh              # Start/stop script
├── Dockerfile           # Railway deploy
├── railway.json         # Railway config
├── Procfile             # Start command
├── .env.example         # Config template
├── .gitignore
├── bot/
│   ├── scanner.py       # DexScreener polling + filters
│   ├── checker.py       # RugCheck safety
│   ├── telegram.py      # Alert sender
│   ├── tracker.py       # 2x outcome monitoring
│   ├── learner.py       # Self-learning engine
│   ├── listener.py      # /start welcome handler
│   ├── models.py        # SQLite schema
│   ├── helius.py        # Solana RPC client
│   ├── wide_scanner.py  # $10M wide net
│   ├── smart_money.py   # Wallet profiler/scoring
│   ├── insider_tracker.py # Insider sell detection
│   ├── whale_watcher.py   # Whale wallet alerts
│   └── backtest.py      # Backtesting engine
├── dashboard/
│   ├── server.py        # HTTP dashboard server
│   └── templates/
│       └── index.html   # Dashboard UI
└── data/
    └── bot.db           # SQLite database
```

---

## 🔌 APIs Used

| API | Cost | Purpose |
|---|---|---|
| DexScreener | Free | Token data, prices, volume, MCap |
| RugCheck | Free | LP lock, risk score, holder data |
| Telegram Bot | Free | Alerts + commands |
| Solana RPC | Free (public) | On-chain early buyer data |

---

## 🗺️ Phase 3 — Roadmap

Planned for when the bot achieves **90%+ success rate**:

### 🤖 Auto-Trade (Jupiter API)
- Auto-buy SOL→token when alert fires
- Auto-sell at 2x take-profit
- Stop-loss at configurable threshold
- Slippage + priority fee configuration

### 👥 Copy Trade
- Mirror smart wallet buys automatically
- Configurable trade size (% of wallet)
- Multi-wallet follow mode

### 👛 Self-Created Wallets
- On-chain wallet generation for new users
- Per-user balance tracking
- Deposit/withdraw SOL

### 💎 Premium Subscription Plans
- **Free Tier** — Alerts only
- **Pro Tier** — Auto-trade + copy trade
- **Whale Tier** — Priority alerts, API access, multi-chain

### 📱 WhatsApp Bot
- Dual Telegram + WhatsApp alerts
- WhatsApp incoming command support

---

## 📊 Current Status

- **Success Rate:** Tracking since first alert
- **GitHub:** [Thomasyoung113/memecoin-alert-bot](https://github.com/Thomasyoung113/memecoin-alert-bot)
- **Telegram:** [@Gemssalert_bot](https://t.me/Gemssalert_bot)
- **Dashboard:** `http://127.0.0.1:8080`

---

## 🛡️ Security

- `.env` is `chmod 600` — only owner can read
- Dashboard binds to `127.0.0.1` only
- Bearer token required for `/api/*` endpoints
- `hmac.compare_digest()` for token comparison
- `html.escape()` on all user-controlled data
- Chat ID whitelist enforced on all commands
- SQLite uses parameterized queries (no SQL injection)

---

*Built with Python + DexScreener API + RugCheck API*