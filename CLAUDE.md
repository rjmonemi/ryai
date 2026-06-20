# RyAI — context for Claude Code

This file is the handoff. The project was scaffolded in a separate Claude Code session
on a **Windows** PC and pushed here; it is meant to **run on a macOS** machine. Read this
before continuing.

## What this is

A Python bot that auto-replies to incoming iMessages in Ryan's texting style. It:
- polls `~/Library/Messages/chat.db` (read-only) for new incoming messages,
- drafts a short, casual reply with the Claude API (`claude-haiku-4-5-20251001`),
- sends it via AppleScript through the Messages app,
- never auto-replies to a whitelist of close contacts (notifies instead),
- flags anything about plans / money / favors for Ryan to handle manually.

**macOS only.** It depends on `chat.db`, `osascript`/AppleScript, and the Messages app.

## Status (as of 2026-06-16)

- Full codebase is written and committed. Every module compiles.
- **It has never been run against a real `chat.db` or a real Mac yet.** This session is
  likely the first time it touches actual hardware — treat behavior as unverified.

## ⚠️ Do this first: verify Full Disk Access

The intended host is a **school / MDM-managed Mac (Cal Poly Pomona)**. MDM may block
granting Full Disk Access to the terminal, which the bot *requires* to read `chat.db`.
If that grant is blocked, nothing else works — so confirm it before building anything new.

Quick go/no-go check:
```bash
python3 - <<'PY'
import sqlite3, os
p = os.path.expanduser("~/Library/Messages/chat.db")
c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
print("rows:", c.execute("SELECT COUNT(*) FROM message").fetchone()[0])
PY
```
If that errors with "unable to open database file" / "authorization denied":
`System Settings → Privacy & Security → Full Disk Access` → enable the terminal app,
then **fully quit and reopen** the terminal. If it can't be enabled at all, the project
is blocked on this Mac (a personal Mac mini was the fallback plan).

## Run it

```bash
python3 -m pip install -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."   # never commit this; read from env only
python3 main.py
```
Persistent run: use `tmux` (see README.md). Messages app must be open and signed in.

## Files

| File | Role |
|---|---|
| `main.py` | main loop, per-chat scheduling, whitelist + flag handling |
| `config.py` | API key, whitelist, all tunable knobs |
| `db.py` | read-only chat.db queries, Apple-epoch conversion, attributedBody decode |
| `contacts.py` | handle → contact name via Contacts app (cached) |
| `claude_api.py` | Claude call + Ryan persona system prompt + JSON parsing |
| `messenger.py` | send iMessages via AppleScript |
| `notifier.py` | macOS notifications |
| `timing.py` | X (rapid) / Y (baseline) delay logic |

## Design decisions already made (don't undo without reason)

- **Scheduler, not blocking sleeps.** The loop ticks every `TICK_SECONDS` and fires
  replies when their `scheduled_time` arrives, so multiple conversations are handled
  concurrently instead of the process sleeping 3–12 min per reply.
- **Two timing modes** (`timing.py`, `main.schedule`):
  - *X / rapid:* ≥3 incoming msgs in 60s (a burst) → wait for a ~10s pause (capped at 4 min), then reply fast.
  - *Y / baseline:* normal cadence → randomized 3–12 min delay, set once (not reset on each msg).
- **Cancels a pending reply if Ryan replies to that chat himself** (avoids talking over him).
- **Group chats skipped by default** (`REPLY_TO_GROUPS = False`).
- **attributedBody decoding** in `db.py`: modern macOS often stores message text in a
  binary blob, not the `text` column. Decode is best-effort; undecodable msgs are skipped.
- **No backlog replies on startup** — only messages that arrive after launch get answered.

## Behavioral constraints (the persona — in `claude_api.SYSTEM_PROMPT`)

- natural, professional tone (no "yo"/slang), 1–3 sentences, no assistant-speak.
- never formally commit to plans; never agree to money; stay non-committal on favors.
- **draft-and-notify** (`config.ASSIST_CONTACTS`: tiara, dad, mom) → never auto-sent; the
  bot drafts a reply and notifies Ryan so he reviews and sends it himself.
- whitelist (`config.WHITELIST`: haya, marcus) → no auto-reply and no draft, just a notification.
- plans/money/favor → still send a deflecting reply, AND set `flag` → notification.

## Open TODOs / next steps

- [ ] **`test_db.py`** — a small go/no-go script (read chat.db, print last few messages)
      to confirm Full Disk Access cleanly before running the whole bot.
- [ ] Feed a bit of **per-contact conversation history** to Claude (currently it only
      sees the latest incoming message(s)) for more in-character replies.
- [ ] Tune `WHITELIST` once observed live — keep it generous (anyone who'd notice a fake reply).
- [ ] Consider: log to a file, and persist processed message IDs so a restart doesn't
      re-reply to recent messages (currently the "ignore backlog before startup" guard
      handles restarts conservatively).

## Conventions

- Run with `python3`. Single dependency: `anthropic` (see `requirements.txt`).
- API key only ever via `ANTHROPIC_API_KEY` env var. `.gitignore` blocks `.env` files.
- Model: `claude-haiku-4-5-20251001` (chosen for cost; ~$2–6/mo expected).
