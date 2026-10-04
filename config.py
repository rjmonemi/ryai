"""Configuration and constants for RyAI."""
import os

# --- Claude API ---
# Set this in your shell:  export ANTHROPIC_API_KEY="sk-ant-..."
# (or paste it as the fallback string below, but env var is safer)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 300
API_TIMEOUT_SECONDS = 30        # give up on a hung API call instead of freezing the bot

# --- iMessage database ---
CHAT_DB_PATH = os.path.expanduser("~/Library/Messages/chat.db")

# --- Who gets what ---
# Both lists match against the contact's name from the Contacts app. You can also put a
# phone number ("+19095551234") or an iMessage email in either list.
#
# WHITELIST: these people NEVER get an auto-reply; you get a notification instead.
# Matching is case-insensitive and partial: "haya" also matches "Haya Ahmed".
# Keep this GENEROUS — anyone close enough to notice a fake reply belongs here.
WHITELIST = [
    "haya",
    "marcus",
]

# INSTANT_REPLY: these people get an answer as soon as the bot sees their text, instead
# of the 3-12 min wait. Matched as a whole word: "mom" matches "Mom" or "Mom ❤️" but not
# "Momo". If someone is on both lists, WHITELIST wins.
INSTANT_REPLY = [
    "tiara",
    "dad",
    "mom",
]

# --- Polling ---
POLL_INTERVAL_SECONDS = 15      # how often we check chat.db for new texts
TICK_SECONDS = 1                # main-loop granularity for firing scheduled replies
LOOKBACK_SECONDS = 120          # each check looks at texts from the last N seconds

# --- Convo mode (rapid back-and-forth) ---
RAPID_FIRE_COUNT = 3            # >= this many texts from them ...
RAPID_FIRE_WINDOW = 60          # ... within this many seconds starts a convo
RAPID_PAUSE_SECONDS = 10        # in a convo, reply once they've gone quiet this long
RAPID_MAX_WAIT_SECONDS = 240    # but never wait longer than this once a reply is owed
CONVO_IDLE_SECONDS = 300        # convo mode ends after this long with no texts either way

# --- Baseline mode (everyone else, normal pace) ---
BASELINE_MIN_SECONDS = 180      # 3 minutes
BASELINE_MAX_SECONDS = 720      # 12 minutes

# --- When you text someone yourself ---
TAKEOVER_SECONDS = 600          # bot stays out of that chat for 10 min after you text in it

# --- Making replies feel human ---
HISTORY_MESSAGES = 20           # recent messages from the chat shown to Claude for context
MAX_REPLY_TEXTS = 3             # a reply can be split into up to this many separate texts
TYPING_BASE_SECONDS = 1.0       # pause before each extra text of a split reply ...
TYPING_SECONDS_PER_CHAR = 0.12  # ... plus this much per character, like typing it out
TYPING_MAX_SECONDS = 6.0
FORCE_LOWERCASE = True          # ryan texts in all lowercase

# --- Behavior ---
REPLY_TO_GROUPS = False         # group chats are skipped by default (auto-replying to them is risky)
