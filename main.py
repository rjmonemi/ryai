"""RyAI - iMessage auto-reply bot. Entry point and main loop."""
import re
import sqlite3
import sys
import time
from collections import deque, namedtuple

import config
import contacts
import claude_api
import db as dbmod
import messenger
import notifier
import timing

ECHO_WINDOW_SECONDS = 300   # how long we remember our own sends, to recognize them in chat.db
STALE_REPLY_SECONDS = 1800  # never answer a text this old (e.g. the Mac slept when the reply was due)
STALE_SEND_SECONDS = 120    # drop a queued follow-up text this overdue (the Mac slept mid-reply)
SLEEP_GAP_SECONDS = 60      # a gap this long between ticks means the Mac slept or the loop stalled

# A text waiting to go out. The 2nd/3rd part of a split reply waits a few "typing" seconds.
Outgoing = namedtuple("Outgoing", "send_at key text mode")


def log(msg):
    print(f"[{time.strftime('%I:%M:%S %p').lower()}] {msg}", flush=True)


class Chat:
    """Everything we track about one person, kept across replies.

    Keyed by their normalized handle, so an iMessage chat and an SMS chat with the same
    number share one Chat.
    """

    def __init__(self, key, handle):
        self.key = key
        self.handle = handle               # where replies go
        self.chat_identifier = None        # for reading the chat history
        self.service = None                # "iMessage" / "SMS" / "RCS", from their latest text
        self.name = None                   # contact name, or the handle if they aren't a contact
        self.instant = False               # on the INSTANT_REPLY list
        self.unreplied = []                # [(time, text)] their texts we haven't answered yet
        self.recent_in = deque(maxlen=20)  # times of their recent texts, for convo detection
        self.last_in = 0.0                 # time of their newest text
        self.first_seen = None             # when we noticed the oldest unanswered text
        self.scheduled_time = None         # when the reply fires
        self.mode = None                   # "baseline", "convo" or "instant"
        self.convo_until = 0.0             # convo mode is on while now < convo_until
        self.takeover_until = 0.0          # you texted them yourself; bot stays out until then
        self.sent = deque()                # [(time, normalized text)] our sends, to spot their echo
        self.warned = False                # already logged that we can't tell who this is

    def label(self):
        return self.name or self.handle or self.key

    def in_convo(self, now):
        return now < self.convo_until

    def clear_pending(self):
        self.unreplied = []
        self.first_seen = None
        self.scheduled_time = None
        self.mode = None


def _norm(text):
    return " ".join(text.lower().split()) if text else ""


def _matches(entry, name, handle, whole_word):
    """Does one WHITELIST / INSTANT_REPLY entry (a name, phone number or email) match this person?"""
    entry = entry.strip().lower()
    if not entry:
        return False
    is_phone = re.fullmatch(r"[\d\s().+-]+", entry) and len(re.sub(r"\D", "", entry)) >= 7
    if "@" in entry or is_phone:
        return bool(handle) and contacts.normalize(handle) == contacts.normalize(entry)
    low = (name or "").lower()
    if whole_word:
        return re.search(r"(?<!\w)" + re.escape(entry) + r"(?!\w)", low) is not None
    return entry in low


def is_whitelisted(name, handle=None):
    """Never auto-reply to these people. Partial match: "haya" matches "Haya Ahmed"."""
    return any(_matches(w, name, handle, whole_word=False) for w in config.WHITELIST)


def is_instant(name, handle=None):
    """Reply to these people right away. Whole-word match: "mom" matches "Mom", not "Momo"."""
    return any(_matches(w, name, handle, whole_word=True) for w in config.INSTANT_REPLY)


def is_group(chat_identifier):
    return bool(chat_identifier) and chat_identifier.startswith("chat")


def is_automated(handle):
    """Short codes (verification codes, delivery alerts) and business chats: never worth a reply."""
    if handle.startswith("urn:"):
        return True
    if "@" in handle:
        return False
    return len(re.sub(r"\D", "", handle)) <= 6


def _preview(text, limit=60):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _duration(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


class Bot:
    """Watches chat.db and decides who gets a reply, and when. Drive it by calling step(now)."""

    def __init__(self, chat_db, start_time):
        self.db = chat_db
        self.start_time = start_time
        self.chats = {}          # person key -> Chat
        self.outbox = []         # Outgoing texts waiting for their send time
        self.name_takeover = {}  # contact name -> takeover end, so it covers all their handles
        self.cursor = chat_db.max_rowid()  # only rows added from now on get polled
        self.last_poll = 0.0
        self.last_step = start_time
        self._remember_recent_takeovers(start_time)

    def step(self, now):
        """One tick of the main loop: check for new texts, send what's due, start due replies."""
        if now < self.last_poll:
            self.last_poll = float("-inf")  # the clock went backwards; poll now instead of stalling
        if now - self.last_step > SLEEP_GAP_SECONDS:
            log(f"[wake] no ticks for {_duration(now - self.last_step)} (Mac asleep?) - catching up")
        self.last_step = now
        if now - self.last_poll >= config.POLL_INTERVAL_SECONDS:
            self.poll(now)
        self.flush_outbox(now)  # texts queued on an earlier tick go out after one more look
        self.fire_due(now)      # replies that are due get written now and go out next tick

    # ---- reading chat.db ----

    def poll(self, now):
        self.last_poll = now
        try:
            newest = self.db.max_rowid()
            if newest < self.cursor:
                self.cursor = newest  # rows were deleted; don't skip what comes next
            rows = self.db.new_messages(self.cursor, newest)
        except Exception as e:
            log(f"[db] query error: {e}")
            return
        self.cursor = newest
        for row in rows:
            try:
                self._handle_row(row, now)
            except Exception as e:
                log(f"[poll] error handling a message: {e}")

    def _chat_for(self, row):
        """The Chat for a message row, or None if it isn't a 1:1 chat we can place."""
        if row["chat_id"] is None:
            return None  # no chat attached: can't tell which conversation this is, so leave it
        if is_group(row["chat_identifier"]) and not config.REPLY_TO_GROUPS:
            return None
        handle = row["handle_id"] or row["chat_identifier"]
        key = contacts.normalize(handle)
        if not key:
            return None
        chat = self.chats.get(key)
        if chat is None:
            chat = self.chats[key] = Chat(key, handle)
        return chat

    def _handle_row(self, row, now):
        chat = self._chat_for(row)
        if chat is None:
            return
        sent_at = dbmod.apple_to_unix(row["date"])
        text = dbmod.message_text(row)
        if row["is_from_me"]:
            reaction = bool(row["associated_message_type"])  # the bot never sends tapbacks
            if reaction or not self._is_echo(chat, text, bool(row["cache_has_attachments"]), sent_at):
                self._take_over(chat, sent_at, now)
        elif sent_at >= self.start_time and now - sent_at <= config.MAX_TEXT_AGE_SECONDS:
            self._from_them(chat, row, text, sent_at, now)
        # else: from before the bot started, or already old when we saw it: left for you

    def _remember_recent_takeovers(self, now):
        """A restart shouldn't forget that you were just texting someone: replay the last few
        minutes of texts sent from your account (by you, or by the bot before the restart)."""
        try:
            rows = self.db.recent_messages(now - config.TAKEOVER_SECONDS)
        except Exception as e:
            log(f"[db] couldn't read recent texts: {e}")
            return
        for row in rows:
            chat = self._chat_for(row) if row["is_from_me"] else None
            if chat is not None:
                self._take_over(chat, dbmod.apple_to_unix(row["date"]), now, quiet=True)
        for chat in self.chats.values():
            if self._in_takeover(chat, now):
                log(f"[you] {chat.label()} got a text from your account in the last few minutes - "
                    f"bot stays out of that chat for {_duration(chat.takeover_until - now)}")

    def _is_echo(self, chat, text, has_attachment, sent_at):
        """Is this from-me row just a text the bot sent? (Messages logs our sends in chat.db too.)

        Compared against the row's own timestamp, so it still works if the Mac slept before
        the row got read.
        """
        while chat.sent and sent_at - chat.sent[0][0] > ECHO_WINDOW_SECONDS:
            chat.sent.popleft()
        if not chat.sent:
            return False
        if text is None:
            # Couldn't read the text. The bot never sends attachments, so a photo is you; anything
            # else right after one of our sends is most likely that send.
            if not has_attachment and abs(sent_at - chat.sent[-1][0]) <= 60:
                chat.sent.popleft()
                return True
            return False
        norm = _norm(text)
        for i, (_, sent_norm) in enumerate(chat.sent):
            if sent_norm == norm:
                del chat.sent[i]
                return True
        return False

    def _in_takeover(self, chat, now):
        until = chat.takeover_until
        if chat.name:
            until = max(until, self.name_takeover.get(chat.name, 0.0))
        return now < until

    def _drop(self, chat):
        """Forget any reply owed to this chat, including texts already queued to go out."""
        chat.clear_pending()
        self.outbox = [o for o in self.outbox if o.key != chat.key]

    def _take_over(self, chat, sent_at, now, quiet=False):
        """You texted (or reacted in) this chat yourself: drop anything pending and stay out of it,
        so the bot never talks over you."""
        until = sent_at + config.TAKEOVER_SECONDS
        if now >= until:
            return  # an old text that only just synced over; it doesn't change anything now
        was_out = self._in_takeover(chat, now)
        chat.takeover_until = max(chat.takeover_until, until)
        if chat.name is None:
            chat.name = contacts.resolve(chat.handle)
        same_person = [chat]
        if chat.name and chat.name != chat.handle:
            # A real contact: also cover their other numbers / emails (other chats).
            self.name_takeover[chat.name] = max(self.name_takeover.get(chat.name, 0.0), until)
            same_person += [c for c in self.chats.values() if c is not chat and c.name == chat.name]
        for c in same_person:
            self._drop(c)
            c.convo_until = 0.0
        if not was_out and not quiet:
            log(f"[you] you texted {chat.label()} yourself - bot stays out of that chat "
                f"for {config.TAKEOVER_SECONDS // 60} min")

    def _from_them(self, chat, row, text, sent_at, now):
        handle = row["handle_id"] or row["chat_identifier"]
        if is_automated(handle):
            return
        chat.handle = handle
        chat.chat_identifier = row["chat_identifier"]
        chat.service = row["service"] or chat.service
        chat.name = contacts.resolve(handle)
        if chat.name is None:
            # Contacts can't be read, so there's no telling whether they're on the whitelist.
            # Don't risk it.
            if text and is_whitelisted(None, handle):  # a whitelisted phone number still works
                notifier.notify("RyAI", f"{handle} texted you", text[:120])
            elif not chat.warned:
                log(f"[contacts] can't read Contacts, so not replying to {handle} "
                    f"(they might be on your whitelist)")
                chat.warned = True
            self._drop(chat)
            return
        chat.warned = False
        chat.instant = is_instant(chat.name, handle)
        if is_whitelisted(chat.name, handle):
            if text:
                notifier.notify("RyAI", f"{chat.name} texted you", text[:120])
            self._drop(chat)  # in case a reply was queued before we knew who this was
            return
        if self._in_takeover(chat, now):
            return  # you're texting them yourself; leave it to you

        # Every real text counts toward convo mode, even a photo with no caption.
        chat.recent_in.append(sent_at)
        chat.last_in = max(chat.last_in, sent_at)
        if timing.is_rapid_fire(chat.recent_in) or sent_at < chat.convo_until:
            if not chat.in_convo(now):
                log(f"[convo] {chat.name} is texting fast - convo mode on "
                    f"(replying ~{config.RAPID_PAUSE_SECONDS}s after they stop)")
            chat.convo_until = max(chat.convo_until, sent_at + config.CONVO_IDLE_SECONDS)

        if text is None:
            return  # a photo / attachment with no text: nothing to answer
        log(f"[in] {chat.name}: {_preview(text)}")
        chat.unreplied.append((sent_at, text))
        if chat.first_seen is None:
            chat.first_seen = now
        self._schedule(chat, now)

    def _schedule(self, chat, now):
        """(Re)compute when this chat's reply fires."""
        if chat.in_convo(now) or chat.instant:
            # Convo: wait until they've gone quiet for a few seconds. Instant: go right away.
            # Either way, don't make them wait forever if they never pause.
            convo = chat.in_convo(now)
            pause = config.RAPID_PAUSE_SECONDS if convo else 0
            cap = chat.first_seen + config.RAPID_MAX_WAIT_SECONDS
            chat.mode = "convo" if convo else "instant"
            chat.scheduled_time = max(now, min(chat.last_in + pause, cap))
        elif chat.mode != "baseline" or chat.scheduled_time is None:
            # Baseline: pick a random 3-12 min delay once; more texts don't push it back.
            chat.mode = "baseline"
            chat.scheduled_time = now + timing.baseline_delay()
            log(f"[queued] {chat.name}: replying in {_duration(chat.scheduled_time - now)}")

    # ---- replying ----

    def _may_text(self, chat, now):
        """Last safety check before writing or sending anything to this chat."""
        if chat.name is None or self._in_takeover(chat, now):
            return False
        return not is_whitelisted(chat.name, chat.handle)

    def fire_due(self, now):
        """Write a reply for every chat whose reply time has come."""
        busy = {o.key for o in self.outbox}  # still sending the extra texts of an earlier reply
        due = [c for c in self.chats.values()
               if c.scheduled_time is not None and now >= c.scheduled_time and c.key not in busy]
        if not due:
            return
        # Check chat.db once more right before answering, so we never reply over a text that just
        # came in: it either gets folded into this reply or pushes the reply back.
        if now > self.last_poll:
            self.poll(now)
        for chat in due:
            if chat.scheduled_time is None or now < chat.scheduled_time:
                continue  # a new text pushed it back, or you took over
            if not chat.unreplied:
                chat.clear_pending()
                continue
            self._reply(chat, now)

    def _reply(self, chat, now):
        texts = [t for _, t in chat.unreplied]
        newest = max(t for t, _ in chat.unreplied)
        mode = chat.mode
        chat.clear_pending()  # these texts are handled now, whatever happens next
        chat.name = contacts.resolve(chat.handle)  # who this is may have changed since it was queued
        if not self._may_text(chat, now):
            return
        if now - newest > STALE_REPLY_SECONDS:
            log(f"[skip] {chat.name}: their text is {_duration(now - newest)} old by now - leaving it for you")
            return
        try:
            result = claude_api.generate_reply(chat.name, self._history(chat), texts, now,
                                               unknown=(chat.name == chat.handle))
        except Exception as e:
            log(f"[claude] error for {chat.name}: {e}")
            return
        if result is None:
            log(f"[claude] no usable reply for {chat.name}; skipping")
            return
        if result["flag"]:
            notifier.notify("RyAI - check this one", f"{chat.name} - you should handle this",
                            "\n".join(texts)[:120])
        if result["skip"]:
            log(f"[skip] {chat.name}: no reply" + (" (flagged for you)" if result["flag"] else ""))
            return
        # Queued, not sent: the next tick takes one more look at chat.db first, in case you
        # texted them yourself while Claude was writing.
        send_at = now
        for i, text in enumerate(result["texts"]):
            if i:
                send_at += timing.typing_delay(text)
            self.outbox.append(Outgoing(send_at, chat.key, text, mode))
        if chat.in_convo(now):
            chat.convo_until = max(chat.convo_until, now + config.CONVO_IDLE_SECONDS)

    def _history(self, chat):
        """Recent messages with this person, oldest first, as context for Claude."""
        try:
            rows = self.db.chat_history(chat.chat_identifier or chat.handle, config.HISTORY_MESSAGES)
        except Exception as e:
            log(f"[db] history error: {e}")
            return []
        history = []
        for row in rows:
            text = dbmod.message_text(row)
            if text is None:
                if not row["cache_has_attachments"]:
                    continue
                text = "[sent a photo or attachment]"
            if len(text) > 300:
                text = text[:300] + "..."
            history.append({"time": dbmod.apple_to_unix(row["date"]),
                            "from_me": bool(row["is_from_me"]),
                            "text": text})
        return history

    def flush_outbox(self, now):
        """Send queued texts whose time has come."""
        if not any(o.send_at <= now for o in self.outbox):
            return
        if now > self.last_poll:
            self.poll(now)  # one last look first: if you just jumped into the chat, this cancels it
        for item in sorted((o for o in self.outbox if o.send_at <= now), key=lambda o: o.send_at):
            if item not in self.outbox:
                continue  # dropped: an earlier part failed, or you took over
            self.outbox.remove(item)
            chat = self.chats.get(item.key)
            if chat is None or not self._may_text(chat, now):
                continue
            if now - item.send_at > STALE_SEND_SECONDS:
                # The Mac slept partway through a split reply; the rest would be out of context.
                self.outbox = [o for o in self.outbox if o.key != item.key]
                continue
            if messenger.send(chat.handle, item.text, chat.service):
                chat.sent.append((now, _norm(item.text)))
                log(f"[sent -> {chat.name} ({item.mode})] {item.text}")
            else:
                # don't send the rest of a reply whose first part didn't go out
                self.outbox = [o for o in self.outbox if o.key != item.key]


def banner():
    print("RyAI running. watching for messages...  (ctrl-c to stop)")
    print(f"  checking for new texts every {config.POLL_INTERVAL_SECONDS}s")
    print(f"  instant replies: {', '.join(config.INSTANT_REPLY) or 'nobody'}")
    print(f"  never auto-reply (you get a notification): {', '.join(config.WHITELIST) or 'nobody'}")
    print(f"  convo mode: {config.RAPID_FIRE_COUNT}+ texts in {config.RAPID_FIRE_WINDOW}s -> "
          f"reply {config.RAPID_PAUSE_SECONDS}s after they stop")
    print(f"  everyone else: {config.BASELINE_MIN_SECONDS // 60}-{config.BASELINE_MAX_SECONDS // 60} min")
    print(f"  contacts loaded: {contacts.entry_count()} phone numbers / emails")


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

    problem = contacts.load()
    if problem:
        print(f"ERROR: {problem}")
        print("  The bot needs Contacts to know who's on your whitelist, so it won't run without it.")
        print("  If macOS asked whether your terminal can access Contacts, click OK and run this again.")
        print("  Otherwise: System Settings -> Privacy & Security -> Automation -> your terminal ->")
        print("  turn on Contacts (and check Privacy & Security -> Contacts too).")
        sys.exit(1)

    problem = claude_api.check_api_key()
    if problem:
        print(f"ERROR: {problem}")
        sys.exit(1)

    banner()
    bot = Bot(chat_db, time.time())
    try:
        while True:
            try:
                bot.step(time.time())
            except Exception as e:
                log(f"[loop] unexpected error: {e}")
            time.sleep(config.TICK_SECONDS)
    except KeyboardInterrupt:
        print("\nstopping RyAI.")
    finally:
        chat_db.close()


if __name__ == "__main__":
    main()
