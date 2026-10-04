# RyAI — iMessage Auto-Reply

A Python bot that watches incoming iMessages, drafts replies in Ryan's texting style
with the Claude API, and sends them automatically. Close contacts can get instant
replies, and a whitelist of people never gets an auto-reply at all.

> **macOS only.** It depends on `chat.db`, AppleScript (`osascript`), and the Messages
> app. It will not run on Windows or Linux. Write it anywhere; run it on the Mac.

---

## What it does

- Checks `~/Library/Messages/chat.db` every 15s for new texts.
- Decides *when* to reply:
  - **Instant (Tiara, Dad, Mom):** replies as soon as it sees their text.
  - **Convo:** if someone sends 3+ texts within a minute, a convo starts. It waits until
    they've been quiet for 10s, then replies, and keeps replying that way until the chat
    goes quiet for 5 minutes.
  - **Baseline (everyone else):** replies after a random 3–12 min delay.
- Shows Claude (`claude-haiku-4-5-20251001`) the last ~20 messages of the chat plus the
  current day/time, so replies fit the conversation. Replies are lowercase, short, casual,
  and sometimes split into two texts like a person would send them.
- Doesn't reply when no reply is needed ("ok", "👍", "night", verification codes, spam).
- **Leaves it to you (notification, no reply):** emergencies / bad news / someone upset,
  and anyone asking whether they're texting a bot.
- **Whitelist (Haya, Marcus):** never get an auto-reply — you get a macOS notification
  so you can answer them yourself.
- **Flag:** plans / money / favors still get a non-committal reply *and* a notification
  so you can take over.
- **You take over:** the moment you text someone yourself (phone or Mac), the bot drops
  any pending reply to them and stays out of that chat for 10 minutes.
- Ignores tapbacks/reactions, group chats, photos with no caption, and short-code senders.

---

## Setup (on the MacBook)

### 1. Install Python 3
```bash
python3 --version      # 3.9+ is fine
```

### 2. Install the dependency
```bash
cd ryai
python3 -m pip install -r requirements.txt
```

### 3. Set your Claude API key
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
```
(Add that line to `~/.zshrc` so it survives reboots, or paste the key into `config.py`.)
The bot checks the key when it starts and tells you right away if it's wrong.

### 4. Grant Full Disk Access  ← the step that may be blocked on a managed Mac
`System Settings → Privacy & Security → Full Disk Access` → enable your terminal app
(Terminal or iTerm). **Fully quit and reopen the terminal afterward** or the grant
won't take effect.

> If you can't toggle this on (school/MDM-managed Mac), the bot can't read `chat.db`
> and nothing else matters — check this first.

### 5. Make sure Messages is open and signed in
AppleScript sends through the running Messages app. The first time the bot sends a text
(or looks up a contact name), macOS asks whether your terminal may control **Messages**
(and **Contacts**). Click **OK**. If you clicked "Don't Allow", turn it back on in
`System Settings → Privacy & Security → Automation`.

### 6. Keep the Mac awake
`System Settings → Battery / Lock Screen` → prevent sleep while plugged in.

---

## Run it

Plain:
```bash
python3 main.py
```

Persistent (survives closing the terminal) — recommended:
```bash
# tmux
tmux new -s ryai
python3 main.py
# detach: Ctrl-b then d   |   reattach: tmux attach -t ryai
```

You'll see lines like:
```
[03:42:10 pm] [in] Mom: are you coming sunday
[03:42:12 pm] [sent -> Mom (instant)] probably, ill let you know
[03:50:31 pm] [convo] Alex is texting fast - convo mode on (replying ~10s after they stop)
[03:50:42 pm] [sent -> Alex (convo)] lmao
[03:50:45 pm] [sent -> Alex (convo)] nah im good
```

After changing `config.py`, stop the bot with **Ctrl-C** and start it again.

---

## Customize

Everything tunable lives in `config.py`:

| Setting | What it controls |
|---|---|
| `INSTANT_REPLY` | people who get a reply right away (whole-word match on the contact name, or a phone number / email) |
| `WHITELIST` | people who never get auto-replies (partial, case-insensitive; also accepts numbers / emails) |
| `POLL_INTERVAL_SECONDS` | how often it checks for new texts (15s) |
| `RAPID_FIRE_COUNT/WINDOW` | what starts a convo (3 texts in 60s) |
| `RAPID_PAUSE_SECONDS` | in a convo, how long they have to go quiet before it replies (10s) |
| `CONVO_IDLE_SECONDS` | how long a convo lasts with no texts before it's back to normal (5 min) |
| `BASELINE_MIN/MAX_SECONDS` | the 3–12 min reply window for everyone else |
| `TAKEOVER_SECONDS` | how long the bot stays out of a chat after you text in it (10 min) |
| `HISTORY_MESSAGES` | how many recent messages Claude sees for context |
| `MAX_REPLY_TEXTS` / `TYPING_*` | splitting a reply into separate texts, and the pause between them |
| `MODEL` / `MAX_TOKENS` | Claude model + reply length cap |
| `REPLY_TO_GROUPS` | off by default — group chats are skipped |

Ryan's persona / tone / rules live in `SYSTEM_PROMPT` in `claude_api.py`.

---

## Files

```
ryai/
├── main.py          # main loop: per-chat state, when to reply, takeover, sending
├── config.py        # API key, contact lists, all the knobs
├── db.py            # read-only chat.db queries + Apple-epoch + attributedBody decode
├── contacts.py      # handle -> contact name (one bulk read of Contacts, cached)
├── claude_api.py    # Claude call + persona system prompt + reply parsing/cleanup
├── messenger.py     # send texts via AppleScript (iMessage, falls back to SMS)
├── notifier.py      # macOS notifications
├── timing.py        # baseline delay, convo detection, typing gaps
├── test_bot.py      # offline tests (run anywhere): python3 -m unittest test_bot
├── requirements.txt
└── README.md
```

## Notes / gotchas

- First run only replies to messages that arrive **after** startup (no replying to a backlog).
- `chat.db` timestamps are Apple-epoch nanoseconds; `db.py` handles the conversion.
- Many modern messages store text in `attributedBody` (not `text`); `db.py` decodes it
  best-effort. If a message's text can't be decoded, the bot just skips it.
- Group chats are skipped by default — auto-replying to a group is a good way to get caught.
- Names come from the Contacts app. If Contacts access is denied, names can't be matched,
  so put phone numbers in `WHITELIST` / `INSTANT_REPLY` if you need them to work anyway.
- Notifications only show up on the Mac running the bot.
- The loop never crashes on a single bad message; errors are printed and it continues.
