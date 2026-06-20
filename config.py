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
# These people NEVER get an auto-reply and NO draft is generated. Matching is
# case-insensitive and partial: any entry that appears inside the resolved contact
# name counts. You simply get a notification that they texted, and you answer them.
WHITELIST = [
    "haya",
    "marcus",
]

# --- Draft-and-notify contacts ---
# The bot NEVER auto-sends to these people. When they text, it waits a short beat to
# let them finish, drafts a reply in your style, and notifies you with that draft so
# YOU can review and send it yourself. Use this for the people you'd actually want to
# answer personally (family, partner) — they are talking to you, not to a bot.
ASSIST_CONTACTS = [
    "tiara",
    "dad",
    "mom",
]

# --- Polling ---
POLL_INTERVAL_SECONDS = 15      # how often we query chat.db
TICK_SECONDS = 5                # main-loop granularity for firing scheduled replies
LOOKBACK_SECONDS = 120          # only look at messages from the last N seconds

# --- Variable X: rapid response mode ---
RAPID_FIRE_COUNT = 3            # >= this many incoming msgs ...
RAPID_FIRE_WINDOW = 60          # ... within this many seconds  => rapid mode (a "burst")
RAPID_PAUSE_SECONDS = 10        # wait for a pause this long after their last msg before replying
RAPID_MAX_WAIT_SECONDS = 240    # but never wait longer than this once rapid mode starts

# --- Draft-and-notify timing ---
ASSIST_BATCH_SECONDS = 10       # after an ASSIST_CONTACTS msg, wait this long to batch a
                                # burst, then surface the draft (short, so you can reply fast)

# --- Variable Y: baseline reply mode ---
BASELINE_MIN_SECONDS = 180      # 3 minutes
BASELINE_MAX_SECONDS = 720      # 12 minutes

# --- Behavior ---
REPLY_TO_GROUPS = False         # group chats are skipped by default (auto-replying to them is risky)
