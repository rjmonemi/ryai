# RyAI — context for Claude Code

This file is the handoff. The project was scaffolded in a separate Claude Code session
on a **Windows** PC and pushed here; it is meant to **run on a macOS** machine. Read this
before continuing.

## What this is

A Python bot that auto-replies to incoming iMessages in Ryan's texting style. It:
- polls `~/Library/Messages/chat.db` (read-only) for new incoming messages,
- drafts a short, casual reply with the Claude API (`claude-haiku-4-5-20251001`), using
  the chat's recent history for context,
- sends it via AppleScript through the Messages app,
- replies instantly to a short list of close contacts (`INSTANT_REPLY`),
- never auto-replies to a whitelist of contacts (notifies instead),
- flags anything about plans / money / favors for Ryan to handle manually, and leaves
  serious texts and "is this a bot?" questions to him entirely.

**macOS only.** It depends on `chat.db`, `osascript`/AppleScript, and the Messages app.

## Status (as of 2026-10-04)

- First live run on the Mac (June 2026): Full Disk Access works, new texts are detected,
  the API key works, and macOS showed the "Terminal wants to control Messages" prompt.
  A successful end-to-end send had not been confirmed yet.
- 2026-10-04: timing, persona and contact handling were reworked at Ryan's request
  (instant replies for Tiara/Dad/Mom, 3-texts-in-a-minute convo mode with a 10s wait,
  more human replies). Covered by offline tests (`test_bot.py`); **the new AppleScript
  (bulk Contacts read, SMS fallback) is unverified on a real Mac.**
- An older open PR (#1, branch `draft-and-notify`) took a different approach for the same
  contacts (draft + notify, never send). It conflicts with this design; only one should land.

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
python3 -m pip install -U -r requirements.txt
export ANTHROPIC_API_KEY="sk-ant-..."   # never commit this; read from env only
python3 main.py
```
Persistent run: use `tmux` (see README.md). Messages app must be open and signed in.

## Files

| File | Role |
|---|---|
| `main.py` | `Bot` (driven by `step(now)`) + per-chat `Chat` state: scheduling, echo/takeover, outbox |
| `config.py` | API key, contact lists, all tunable knobs |
| `db.py` | read-only chat.db queries, chat history, Apple-epoch conversion, attributedBody decode |
| `contacts.py` | handle → contact name: one bulk Contacts read, digit-normalized match, cached |
| `claude_api.py` | Claude call + Ryan persona system prompt + reply parsing/cleanup |
| `messenger.py` | send texts via AppleScript (iMessage first, SMS fallback) |
| `notifier.py` | macOS notifications |
| `timing.py` | baseline delay, convo (rapid-fire) detection, typing gaps |
| `test_bot.py` | offline tests: fake chat.db + fake clock, no Mac or API key needed |

## Design decisions already made (don't undo without reason)

- **Scheduler, not blocking sleeps.** The loop ticks every `TICK_SECONDS` (1s) and fires
  replies when their `scheduled_time` arrives, so multiple conversations are handled
  concurrently. Replies wait in an outbox and go out on the *next* tick, after one more
  look at chat.db (so a text Ryan sent while Claude was writing cancels it).
- **Poll by ROWID, not a time window** (`db.new_messages`), so nothing is missed when the
  Mac sleeps or a text syncs over late. Incoming texts already older than
  `MAX_TEXT_AGE_SECONDS` when seen are left for Ryan; replies whose text is >30 min old
  by the time they'd go out are dropped (`main.STALE_REPLY_SECONDS`).
- **Per-person state survives replies** (`main.Chat`, keyed by normalized handle so an
  iMessage chat and an SMS chat with the same number are one person). Takeover also
  applies by contact name, covering someone's phone *and* email.
- **Fail closed on Contacts:** the bot won't start if Contacts can't be read, and never
  auto-replies to someone `contacts.resolve` can't identify (returns None). The whitelist
  is re-checked right before writing and before sending each text.
- **Restart memory:** at startup, texts sent from Ryan's account in the last
  `TAKEOVER_SECONDS` re-create their takeovers.
- **Three timing modes** (`main.Bot._schedule`):
  - *instant:* `INSTANT_REPLY` contacts → reply as soon as the text is seen.
  - *convo:* ≥3 texts from them within 60s (by message timestamps) → reply once they've been
    quiet 10s (capped at 4 min of waiting). Stays on until 5 min with no texts either way.
    An instant contact who is mid-burst also gets the 10s settle.
  - *baseline:* everyone else → randomized 3–12 min delay, set once (not reset on each msg).
- **Re-check chat.db right before replying** (and before each queued follow-up text), so a
  text that just arrived is folded in / pushes the reply back instead of being talked over.
- **Echo detection:** the bot's own sends appear in chat.db as `is_from_me` rows; they're
  matched (by text, against the row's own timestamp) to recently sent texts so they're
  never mistaken for Ryan.
- **Takeover:** when Ryan texts or reacts in a chat himself (phone or Mac), pending/queued
  replies for it are dropped and the bot stays out of that chat for `TAKEOVER_SECONDS`.
- **Group chats skipped by default** (`REPLY_TO_GROUPS = False`). Tapbacks/reactions,
  group events, caption-less photos and short-code senders never trigger a reply.
- **attributedBody decoding** in `db.py`: modern macOS often stores message text in a
  binary blob, not the `text` column. Decode is best-effort; undecodable msgs are skipped.
- **No backlog replies on startup** — only messages that arrive after launch get answered.
- **Contacts:** one bulk read, digits-normalized (contacts are stored formatted, so the old
  "contains the 10 digits" search never matched them). A read with no numbers/emails counts
  as a failure; a refresh failure keeps the previous copy. A number shared by several
  contacts keeps every name. Lists also accept phone numbers/emails.
- **Structured outputs** (`output_config` json_schema) for the reply; if the API ever
  rejects it, `claude_api` falls back to parsing plain JSON for the rest of the run.

## Behavioral constraints (the persona — in `claude_api.SYSTEM_PROMPT`)

- always lowercase, short, casual, matches their energy, no assistant-speak; sometimes
  splits a reply into 2 texts; warmer and cleaner with family.
- never formally commit to plans; never agree to money; stay vague on favors.
- skip (send nothing) when no reply is needed, or the text is automated.
- serious/urgent texts (emergency, bad news, someone upset) and "am I texting a bot / is
  this really ryan?" → **send nothing and flag** so Ryan answers himself. (This replaced
  the old "never reveal it's an AI" rule: the bot shouldn't lie to someone who sincerely asks.)
- whitelist (`config.WHITELIST`: haya, marcus) → no auto-reply, fire a notification.
- instant (`config.INSTANT_REPLY`: tiara, dad, mom) → reply right away. Whitelist wins on overlap.
- plans/money/favor → still send a non-committal reply, AND set `flag` → notification.

## Open TODOs / next steps

- [ ] **`test_db.py`** — a small go/no-go script (read chat.db, print last few messages)
      to confirm Full Disk Access cleanly before running the whole bot.
- [x] Feed per-contact conversation history to Claude (done: last `HISTORY_MESSAGES`).
- [ ] Confirm on the Mac: bulk Contacts read resolves names, SMS fallback works for a
      green-bubble contact, split replies look natural.
- [ ] Persona facts may be stale: it says Ryan is "starting his master's at USC in the
      fall" and works 7am-4pm at SKM. Check with Ryan and update `SYSTEM_PROMPT`.
- [ ] Tune `WHITELIST` once observed live — keep it generous (anyone who'd notice a fake reply).
- [ ] Consider: log to a file, and persist processed message IDs so a restart doesn't
      re-reply to recent messages (currently the "ignore backlog before startup" guard
      handles restarts conservatively).

## Conventions

- Run with `python3` (the Mac has 3.9 — no 3.10+ syntax). Single dependency: `anthropic`
  (>=0.77.0 for `output_config`; the Mac had 0.111.0).
- Tests: `python3 -m unittest test_bot` (runs anywhere; keep them passing).
- API key only ever via `ANTHROPIC_API_KEY` env var. `.gitignore` blocks `.env` files.
- Model: `claude-haiku-4-5-20251001` (chosen for cost; ~$2–6/mo expected).
