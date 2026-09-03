import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")       # your personal chat ID
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")  # channel ID (e.g. -1001234567890)

# ── API base URLs ─────────────────────────────────────────────────────
DEXSCREENER_BASE = "https://api.dexscreener.com"
RUGCHECK_BASE = "https://api.rugcheck.xyz/v1"

# ── Scan intervals (seconds) ──────────────────────────────────────────
POLL_INTERVAL = 30            # How often to check DexScreener for new tokens
OUTCOME_CHECK_INTERVAL = 10            # Every 10s check if alerted tokens hit 2x or -50%
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
SMART_WALLET_MIN_HITS = 5      # Min early buys to be considered smart
SMART_WALLET_MIN_SUCCESS_RATE = 0.8            # Min 80% for "smart" label
FIRST_BUYERS_DEPTH = 500       # How many early buyers to profile

# ── Dashboard auth ──────────────────────────────────────────────────────
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")  # empty string = no auth

# ── Wallet encryption ──────────────────────────────────────────────────
WALLET_ENCRYPTION_KEY = os.getenv("WALLET_ENCRYPTION_KEY", "")

DATABASE_URL = os.getenv("DATABASE_URL", "")

# ── Monetization (Phase 4) ────────────────────────────────────────────
# Channels: the free funnel requires a join before /start unlocks.
GATE_CHANNEL_ID = os.getenv("GATE_CHANNEL_ID", "@everyday_aaii")
PRO_CHANNEL_ID = os.getenv("PRO_CHANNEL_ID", "")   # official Pro channel

TRIAL_HOURS = 72                 # 3-day free trial with full access
GRACE_HOURS = 48                 # lapse → 48h grace before access cut
SUB_PRICE_WEEK_SOL = 0.2         # weekly Pro price in SOL
SUB_PRICE_MONTH_SOL = 0.5        # monthly Pro price in SOL
SUB_PRICE_WEEK_STARS = 150       # Telegram Stars SKUs (decide exact amount)
SUB_PRICE_MONTH_STARS = 350
MIN_SOL_PRICE_RATIO = 0.97       # ≥97% of price auto-activates, below = pro-rata days

PROMO_CODES_PER_DAY = 10         # generated daily, DM'd to the owner
PROMO_DURATION_DAYS = 7          # each code = 1 free week
PROMO_CODE_LEN = 10              # chars, secrets-grade alphabet
PROMO_RATE_LIMIT_PER_DAY = 3     # redemption attempts per user per day
FREE_TOP_CALL_DAYS = 7           # free tier: 1 top call per week
SIGNAL_HOUR_UTC = 13             # daily free-tier message hour
PROMO_GEN_HOUR_UTC = 9           # daily promo batch hour (owner DM)

TREASURY_WALLET_ADDRESS = os.getenv("TREASURY_WALLET_ADDRESS", "")
INVOICE_TTL_MINUTES = 60         # SOL deposit invoice lifetime
PAYMENT_CONFIRMATIONS = 1        # confirmations before Pro activates
