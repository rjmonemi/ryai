"""RyAI - iMessage auto-reply bot. Entry point and main loop."""
import sqlite3
import sys
import time
from collections import deque

import config
import contacts
import claude_api
import db as dbmod
import messenger
import notifier
import timing


class Pending:
    """A reply we intend to send to one chat, once its scheduled_time arrives."""

    def __init__(self, handle, sender_name):
        self.handle = handle
        self.sender_name = sender_name
        self.texts = []            # unreplied incoming message texts, in order
        self.timestamps = deque()  # rolling window of incoming msg unix times
        self.mode = None           # "X", "Y", or "assist"
        self.scheduled_time = None
        self.first_seen = None
        self.assist = False        # draft-and-notify contact: never auto-send


def is_whitelisted(name):
    low = name.lower()
    return any(w in low for w in config.WHITELIST)


def is_assist(name):
    """Draft-and-notify contacts: never auto-send; draft a reply for Ryan to send himself."""
    low = name.lower()
    return any(a in low for a in config.ASSIST_CONTACTS)


def is_group(chat_identifier):
    return bool(chat_identifier) and chat_identifier.startswith("chat")


def schedule(pending, now):
    """(Re)compute when this reply should fire, based on the current cadence."""
    if pending.assist:
        # Draft-and-notify: never auto-send. Wait a short beat to batch a burst,
        # then surface a draft — but never defer past the rapid-mode cap.
        pending.mode = "assist"
        cap = pending.first_seen + config.RAPID_MAX_WAIT_SECONDS
        pending.scheduled_time = min(now + config.ASSIST_BATCH_SECONDS, max(cap, now))
        return
    if timing.is_rapid_fire(list(pending.timestamps), now):
        # Variable X: keep pushing the reply out until they pause, but cap the total wait.
        pending.mode = "X"
        cap = pending.first_seen + config.RAPID_MAX_WAIT_SECONDS
        pending.scheduled_time = min(now + config.RAPID_PAUSE_SECONDS, max(cap, now))
    else:
        # Variable Y: set a randomized delay once; don't keep resetting it.
        if pending.mode != "Y" or pending.scheduled_time is None:
            pending.mode = "Y"
            pending.scheduled_time = now + timing.baseline_delay()


def main():
    if not config.ANTHROPIC_API_KEY:
        print("ERROR: ANTHROPIC_API_KEY is not set.")
        print('  Run:  export ANTHROPIC_API_KEY="sk-ant-..."   (or set it in config.py)')
        sys.exit(1)

    chat_db = dbmod.ChatDB()
    try:
        chat_db.connect()
        chat_db.conn.execute("SELECT COUNT(*) FROM message")  # fail loudly on permissions now
    except sqlite3.OperationalError as e:
        print("ERROR: cannot read the iMessage database.")
        print(f"  ({e})")
        print("  Grant Full Disk Access to your terminal:")
        print("    System Settings -> Privacy & Security -> Full Disk Access -> enable your terminal")
        print("  then FULLY QUIT and reopen the terminal and run this again.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: could not open chat.db at {chat_db.path}: {e}")
        sys.exit(1)

    print("RyAI running. watching for messages...  (ctrl-c to stop)")

    processed = set()      # message ROWIDs we've already seen
    pendings = {}          # chat key -> Pending
    start_time = time.time()
    last_poll = 0.0

    while True:
        try:
            now = time.time()

            # ---- poll chat.db ----
            if now - last_poll >= config.POLL_INTERVAL_SECONDS:
                last_poll = now
                try:
                    rows = chat_db.recent_messages(now - config.LOOKBACK_SECONDS)
                except Exception as e:
                    print(f"[db] query error: {e}")
                    rows = []

                for row in rows:
                    try:
                        mid = row["msg_id"]
                        if mid in processed:
                            continue
                        processed.add(mid)

                        if dbmod.apple_to_unix(row["date"]) < start_time:
                            continue  # ignore backlog from before the bot started

                        chat_id = row["chat_id"]

                        # Ryan answered this chat himself -> cancel any pending auto-reply.
                        if row["is_from_me"]:
                            if chat_id is not None and chat_id in pendings:
                                del pendings[chat_id]
                            continue

                        handle = row["handle_id"]
                        if not handle:
                            continue
                        if is_group(row["chat_identifier"]) and not config.REPLY_TO_GROUPS:
                            continue

                        key = chat_id if chat_id is not None else f"h:{handle}"

                        text = dbmod.message_text(row)
                        if not text:
                            continue

                        name = contacts.resolve(handle)
                        assist = is_assist(name)

                        # Whitelist = notify only, no draft. Assist contacts are handled
                        # below (drafted, never auto-sent), so they skip the whitelist.
                        if not assist and is_whitelisted(name):
                            notifier.notify("RyAI", f"{name} texted you", text[:120])
                            continue

                        p = pendings.get(key)
                        if p is None:
                            p = Pending(handle, name)
                            p.first_seen = now
                            pendings[key] = p
                        p.handle = handle
                        p.sender_name = name
                        p.assist = assist
                        p.texts.append(text)
                        p.timestamps.append(dbmod.apple_to_unix(row["date"]))
                        while p.timestamps and now - p.timestamps[0] > config.RAPID_FIRE_WINDOW:
                            p.timestamps.popleft()
                        schedule(p, now)
                    except Exception as e:
                        print(f"[poll] error handling a message: {e}")
                        continue

            # ---- fire replies that are due ----
            for key in list(pendings.keys()):
                p = pendings[key]
                if p.scheduled_time is None or now < p.scheduled_time:
                    continue
                try:
                    combined = "\n".join(p.texts)
                    result = claude_api.generate_reply(p.sender_name, combined)
                    if result is None:
                        print(f"[claude] no usable reply for {p.sender_name}; skipping")
                        continue
                    if p.assist:
                        # Never auto-send to these contacts — hand Ryan a ready-to-send
                        # draft and let him review and send it himself.
                        preview = combined[:80] + ("..." if len(combined) > 80 else "")
                        notifier.notify(f"RyAI draft -> {p.sender_name}",
                                        preview, result["reply"])
                        print(f"[draft -> {p.sender_name}] {result['reply']}")
                    else:
                        if messenger.send(p.handle, result["reply"]):
                            print(f"[sent -> {p.sender_name} ({p.mode})] {result['reply']}")
                        if result["flag"]:
                            notifier.notify("RyAI - check this one",
                                            f"{p.sender_name} (plans/money/favor)",
                                            combined[:120])
                except Exception as e:
                    print(f"[reply] error for {p.sender_name}: {e}")
                finally:
                    pendings.pop(key, None)

            time.sleep(config.TICK_SECONDS)

        except KeyboardInterrupt:
            print("\nstopping RyAI.")
            break
        except Exception as e:
            print(f"[loop] unexpected error: {e}")
            time.sleep(config.TICK_SECONDS)

    chat_db.close()


if __name__ == "__main__":
    main()
