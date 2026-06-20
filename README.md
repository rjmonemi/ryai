# RyAI — iMessage Auto-Reply

A Python bot that watches incoming iMessages, drafts replies in Ryan's texting style
with the Claude API, and sends them automatically — except for close contacts, who are
either drafted-and-handed-to-you (family/partner) or left entirely for you to answer.

> **macOS only.** It depends on `chat.db`, AppleScript (`osascript`), and the Messages
> app. It will not run on Windows or Linux. Write it anywhere; run it on the Mac.

---

## What it does

- Polls `~/Library/Messages/chat.db` every ~15s for new incoming messages.
- Decides *when* to reply using two modes:
  - **X (rapid):** if someone sends 3+ messages in 60s (a "burst"), it waits for a
    natural pause (~10s after their last message, capped at 4 min) and then replies fast.
  - **Y (baseline):** normal cadence → replies after a randomized 3–12 min delay.
- Asks Claude (`claude-haiku-4-5-20251001`) for a short, natural, professional reply plus a
  `flag` for anything about **plans / money / favors**.
- **Draft-and-notify (Tiara, Dad, Mom):** these contacts are **never** auto-replied to.
  When they text, the bot drafts a reply in your style and notifies you with it — you
  review and send it yourself, so they're always talking to you, not a bot.
- **Whitelist (Haya, Marcus):** never get an auto-reply and no draft — you just get a
  notification so you can answer them yourself.
- **Flag:** flagged messages still get a deflecting reply *and* fire a notification so
  you can take over.
- Cancels a pending auto-reply if **you** reply to that chat first.

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

### 4. Grant Full Disk Access  ← the step that may be blocked on a managed Mac
`System Settings → Privacy & Security → Full Disk Access` → enable your terminal app
(Terminal or iTerm). **Fully quit and reopen the terminal afterward** or the grant
won't take effect.

> If you can't toggle this on (school/MDM-managed Mac), the bot can't read `chat.db`
> and nothing else matters — check this first.

### 5. Make sure Messages is open and signed in
AppleScript sends through the running Messages app.

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

You'll see lines like `[sent -> Alex (Y)] probably, i'll check` as it works.

---

## Customize

Everything tunable lives in `config.py`:

| Setting | What it controls |
|---|---|
| `ASSIST_CONTACTS` | draft-and-notify only — never auto-sent (Tiara, Dad, Mom) |
| `WHITELIST` | names that get no auto-reply and no draft (partial, case-insensitive) |
| `MODEL` / `MAX_TOKENS` | Claude model + reply length cap |
| `BASELINE_MIN/MAX_SECONDS` | the 3–12 min baseline reply window |
| `RAPID_FIRE_COUNT/WINDOW` | what counts as a burst (default 3 msgs / 60s) |
| `RAPID_PAUSE_SECONDS` | how long to wait for a pause before replying in rapid mode |
| `ASSIST_BATCH_SECONDS` | how long to batch a burst before surfacing a draft |
| `REPLY_TO_GROUPS` | off by default — group chats are skipped |

Ryan's persona / tone / rules live in `SYSTEM_PROMPT` in `claude_api.py`.

---

## Files

```
ryai/
├── main.py          # main loop, scheduling, whitelist/flag handling
├── config.py        # API key, whitelist, all the knobs
├── db.py            # read-only chat.db queries + Apple-epoch + attributedBody decode
├── contacts.py      # handle -> contact name (cached)
├── claude_api.py    # Claude call + persona system prompt + JSON parsing
├── messenger.py     # send iMessages via AppleScript
├── notifier.py      # macOS notifications
├── timing.py        # X / Y delay logic
├── requirements.txt
└── README.md
```

## Notes / gotchas

- First run only replies to messages that arrive **after** startup (no replying to a backlog).
- `chat.db` timestamps are Apple-epoch nanoseconds; `db.py` handles the conversion.
- Many modern messages store text in `attributedBody` (not `text`); `db.py` decodes it
  best-effort. If a message's text can't be decoded, the bot just skips it.
- Group chats are skipped by default — auto-replying to a group is a good way to get caught.
- The loop never crashes on a single bad message; errors are printed and it continues.
