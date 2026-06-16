"""Configuration and constants for RyAI."""
import os

# --- Claude API ---
# Set this in your shell:  export ANTHROPIC_API_KEY="sk-ant-..."
# (or paste it as the fallback string below, but env var is safer)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 200

# --- iMessage database ---
CHAT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")

# --- Whitelist ---
# These people NEVER get an auto-reply. Matching is case-insensitive and partial:
# any whitelist entry that appears inside the resolved contact name counts.
# Keep this GENEROUS — anyone close enough to notice a fake reply belongs here.
WHITELIST = [
    "haya",
    "dad",
    "marcus",
]

# --- Polling ---
POLL_INTERVAL_SECONDS = 15      # how often we query chat.db
TICK_SECONDS = 5                # main-loop granularity for firing scheduled replies
LOOKBACK_SECONDS = 120          # only look at messages from the last N seconds

# --- Variable X: rapid response mode ---
RAPID_FIRE_COUNT = 4            # >= this many incoming msgs ...
RAPID_FIRE_WINDOW = 60          # ... within this many seconds  => rapid mode
RAPID_PAUSE_SECONDS = 30        # wait for a pause this long after their last msg before replying
RAPID_MAX_WAIT_SECONDS = 240    # but never wait longer than this once rapid mode starts

# --- Variable Y: baseline reply mode ---
BASELINE_MIN_SECONDS = 180      # 3 minutes
BASELINE_MAX_SECONDS = 720      # 12 minutes

# --- Behavior ---
REPLY_TO_GROUPS = False         # group chats are skipped by default (auto-replying to them is risky)
