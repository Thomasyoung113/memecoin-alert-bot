import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── API base URLs ─────────────────────────────────────────────────────
DEXSCREENER_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"

# ── Scan intervals (seconds) ──────────────────────────────────────────
POLL_INTERVAL = 30            # How often to check DexScreener for new tokens
OUTCOME_CHECK_INTERVAL = 60   # Every 1min check if alerted tokens hit 2x or -50%
LEARN_INTERVAL = 3600         # Every hour re-run learning analysis
WIDE_SCAN_INTERVAL = 600      # Every 10min scan for $10M+ tokens (Phase 2)

# ── Filter thresholds (user's strategy) ───────────────────────────────
LIQUIDITY_MIN = 8500          # Minimum liquidity in USD
MCAP_MIN = 200_000            # Minimum market cap
MCAP_MAX = 1_000_000          # Maximum market cap
MAX_AGE_HOURS = 72            # Max token age
BUYS_24H_MIN = 50             # Min buys in 24h
SELLS_24H_MIN = 30            # Min sells in 24h
TXS_5M_MIN = 10               # Min transactions in 5m
VOL_5M_MIN = 10_000           # Min volume in 5m (USD)

# ── Safety checks ─────────────────────────────────────────────────────
LIQUIDITY_LOCKED_TARGET = 100  # Target % for LP lock

# ── Outcome tracking ──────────────────────────────────────────────────
TWO_X_TARGET = 2.0             # Multiply alert MCap by this for 2x
LOSS_TARGET = 0.5              # Multiplier for -50% loss check (0.5 = half the alert MCap)
OUTCOME_MAX_HOURS = 48         # Stop watching after this many hours
OUTCOME_MAX_CHECKS = int(OUTCOME_MAX_HOURS * 3600 / OUTCOME_CHECK_INTERVAL)

# ── Learning ──────────────────────────────────────────────────────────
MIN_CALLS_FOR_LEARN = 10      # Minimum data points before adjusting filters
FILTERS_TO_TUNE = [            # Which filters the learner can adjust
    "mcap_min", "mcap_max", "vol_5m_min",
    "buys_24h_min", "sells_24h_min", "txs_5m_min",
]
FILTER_ADJUSTMENT_FACTOR = 0.15  # How much to adjust per iteration (15%)

# ── Helius (Phase 2 on-chain data) ─────────────────────────────────────
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY", "")

# ── Whale Watcher ─────────────────────────────────────────────────────
WHALE_WATCH_INTERVAL = 300       # Every 5 minutes check whale wallets
WHALE_WATCH_LIST = [
    # Example public Solana whale wallets (replace with real ones)
    "5WZz6xkYNMhCBPbbvyD1NXdPcDJregWmkL46WHd1QQdC",
    "DgNqfXWshgfFRNyGmKvrKqWmKbRCqWQHKJWFTJfMjojQ",
    "9uPEqRvaq7pn6fm5MvN9kPmSpYwwBfyxnkiRrow1FGGi",
    "B2x2pYZ136xQV3MaiWjH5G5JQTHFSB4KCDrCqESiFPxq",
    "3y3P88pF9RMdCjY7JRFQGVwLY3FyTFiV7rs6qLMMhBx",
]

# ── Smart Money (Phase 2) ─────────────────────────────────────────────
WIDE_SCAN_MCAP_THRESHOLD = 10_000_000
SMART_WALLET_MIN_HITS = 3      # Min early buys to be considered smart
SMART_WALLET_MIN_SUCCESS_RATE = 0.6  # Min 60% for "smart" label
FIRST_BUYERS_DEPTH = 100       # How many early buyers to profile

# ── Dashboard auth ──────────────────────────────────────────────────────
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")  # empty string = no auth

# ── DB path ──────────────────────────────────────────────────────────────
DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = os.path.join(DB_DIR, "bot.db")