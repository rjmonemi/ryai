"""Offline tests for RyAI. Runs anywhere (no Mac, no API key):  python3 -m unittest test_bot

A fake chat.db stands in for ~/Library/Messages/chat.db, and Messages / Contacts / Claude
are swapped for fakes, so the bot's timing decisions can be checked second by second.
"""
import io
import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

import anthropic
import httpx

import claude_api
import config
import contacts
import db as dbmod
import main
import messenger
import timing

T0 = 1_790_000_000.0  # the simulated moment the bot starts

BOB = "+19095550001"      # a friend: baseline timing
MOM = "+19095550002"      # on INSTANT_REPLY
TIARA = "+19095550003"    # on INSTANT_REPLY
DAD = "+19095550004"      # on INSTANT_REPLY (no longer whitelisted)
HAYA = "+19095550009"     # on WHITELIST
NAMES = {BOB: "Bob Smith", MOM: "Mom", TIARA: "Tiara Johnson", DAD: "Dad", HAYA: "Haya Ahmed"}

DEFAULT_REPLY = {"texts": ["ok bet"], "flag": False, "skip": False}


class FakeChatDB:
    """A tiny chat.db with the tables and columns RyAI reads."""

    def __init__(self, path, old_schema=False):
        self.path = path
        self.old_schema = old_schema
        self.conn = sqlite3.connect(path)
        optional = "" if old_schema else """,
            service TEXT, associated_message_type INTEGER DEFAULT 0,
            cache_has_attachments INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0"""
        self.conn.executescript(f"""
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT);
            CREATE TABLE chat (ROWID INTEGER PRIMARY KEY AUTOINCREMENT, guid TEXT, chat_identifier TEXT);
            CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
            CREATE TABLE message (
                ROWID INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, attributedBody BLOB,
                handle_id INTEGER DEFAULT 0, date INTEGER, is_from_me INTEGER DEFAULT 0{optional});
        """)
        self.conn.commit()

    def _handle_rowid(self, handle):
        row = self.conn.execute("SELECT ROWID FROM handle WHERE id = ?", (handle,)).fetchone()
        if row:
            return row[0]
        return self.conn.execute("INSERT INTO handle (id) VALUES (?)", (handle,)).lastrowid

    def _chat_rowid(self, guid, identifier):
        row = self.conn.execute("SELECT ROWID FROM chat WHERE guid = ?", (guid,)).fetchone()
        if row:
            return row[0]
        return self.conn.execute("INSERT INTO chat (guid, chat_identifier) VALUES (?, ?)",
                                 (guid, identifier)).lastrowid

    def add(self, handle, text, at, chat=None, from_me=False, assoc=0, item_type=0,
            attachments=0, service="iMessage", body=None, raw_text=None, joined=True):
        """Add a message row. `chat` is the chat_identifier (defaults to the handle, like a 1:1
        chat); each service gets its own chat row, like chat.db does."""
        identifier = chat or handle
        handle_rowid = self._handle_rowid(handle) if handle else 0
        date = int((at - dbmod.APPLE_EPOCH_OFFSET) * 1e9)
        text_sql = "CAST(? AS TEXT)" if raw_text is not None else "?"
        value = raw_text if raw_text is not None else text
        if self.old_schema:
            cur = self.conn.execute(
                f"INSERT INTO message (text, attributedBody, handle_id, date, is_from_me) "
                f"VALUES ({text_sql}, ?, ?, ?, ?)", (value, body, handle_rowid, date, int(from_me)))
        else:
            cur = self.conn.execute(
                f"INSERT INTO message (text, attributedBody, handle_id, date, is_from_me, service, "
                f"associated_message_type, cache_has_attachments, item_type) "
                f"VALUES ({text_sql}, ?, ?, ?, ?, ?, ?, ?, ?)",
                (value, body, handle_rowid, date, int(from_me), service, assoc, attachments, item_type))
        if joined:
            chat_rowid = self._chat_rowid(f"{service};-;{identifier}", identifier)
            self.conn.execute("INSERT INTO chat_message_join VALUES (?, ?)", (chat_rowid, cur.lastrowid))
        self.conn.commit()
        return cur.lastrowid


class Sim:
    """Runs the real Bot against a fake chat.db with a fake clock, one simulated second at a time."""

    def __init__(self, tmpdir, replies=None, names=None):
        self.now = T0
        self.fake = FakeChatDB(os.path.join(tmpdir, "chat.db"))
        self.chat_db = dbmod.ChatDB(self.fake.path)
        self.chat_db.connect()
        self.bot = None         # created on the first run, like starting the real bot
        self.names = dict(NAMES if names is None else names)
        self.contacts_ok = True
        self.echo_text = True   # whether our own sends show up in chat.db with readable text
        self.replies = list(replies or [])
        self.on_claude = None   # optional hook that runs while "Claude is writing"
        self.sent = []          # (time offset, handle, text)
        self.claude_calls = []
        self.notes = []
        self.events = []        # (time, fn)
        self.send_ok = True
        self.out = io.StringIO()
        self._patches = [
            mock.patch("messenger.send", self._send),
            mock.patch("contacts.resolve", self._resolve),
            mock.patch("claude_api.generate_reply", self._claude),
            mock.patch("notifier.notify", lambda *a: self.notes.append(a)),
            mock.patch("timing.baseline_delay", lambda: 300.0),
            mock.patch("timing.typing_delay", lambda text: 3.0),
        ]
        for p in self._patches:
            p.start()

    def close(self):
        for p in self._patches:
            p.stop()
        self.chat_db.close()
        self.fake.conn.close()

    # fakes for Contacts, Messages and Claude
    def _resolve(self, handle):
        if not self.contacts_ok:
            return None
        return self.names.get(handle, handle)

    def _send(self, handle, text, service=None):
        if not self.send_ok:
            return False
        self.sent.append((self.now - T0, handle, text))
        # Messages logs our own sends in chat.db, just like the real thing.
        self.fake.add(handle, text if self.echo_text else None, self.now, from_me=True)
        return True

    def _claude(self, name, history, new_texts, now, unknown=False):
        self.claude_calls.append({"name": name, "history": history, "texts": list(new_texts),
                                  "at": now - T0, "unknown": unknown})
        if self.on_claude:
            self.on_claude()
        reply = self.replies.pop(0) if self.replies else DEFAULT_REPLY
        if isinstance(reply, Exception):
            raise reply
        return reply

    # scripting the other side of the conversation
    def text(self, offset, handle, text, **kwargs):
        """They text us at T0 + offset."""
        def fire():
            self.fake.add(handle, text, self.now, **kwargs)
        self.events.append((T0 + offset, fire))

    def you_text(self, offset, handle, text, dated=None, **kwargs):
        """You text them yourself (phone or Mac). It lands in chat.db at T0 + offset; `dated`
        backdates it, like a text from your phone that syncs over late."""
        def fire():
            at = T0 + dated if dated is not None else self.now
            self.fake.add(handle, text, at, from_me=True, **kwargs)
        self.events.append((T0 + offset, fire))

    def start(self):
        if self.bot is None:
            with redirect_stdout(self.out):
                self.bot = main.Bot(self.chat_db, T0)

    def run_until(self, offset):
        self.start()
        with redirect_stdout(self.out):
            while self.now < T0 + offset:
                self.now += 1
                for event in [e for e in self.events if e[0] <= self.now]:
                    self.events.remove(event)
                    event[1]()
                self.bot.step(self.now)

    def sleep_until(self, offset):
        """The Mac sleeps: time passes with no ticks at all."""
        self.start()
        self.now = T0 + offset

    def sends_to(self, handle):
        return [(t, text) for t, h, text in self.sent if h == handle]


class SimTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def sim(self, **kwargs):
        s = Sim(self._tmp.name, **kwargs)
        self.addCleanup(s.close)
        return s


class TestTiming(SimTestCase):
    # Polls happen at +1, then every 15s (+16, +31, ...), plus one right before a reply is written
    # and one right before each text goes out. A reply written on one tick is sent on the next.

    def test_normal_text_waits_the_baseline_delay(self):
        s = self.sim()
        s.text(5, BOB, "yo whats up")
        s.run_until(316)
        self.assertEqual(s.sent, [])                       # seen at +16, reply written at +316
        s.run_until(320)
        self.assertEqual(s.sends_to(BOB), [(317, "ok bet")])
        self.assertEqual(s.claude_calls[0]["texts"], ["yo whats up"])

    def test_more_texts_dont_push_the_baseline_back(self):
        s = self.sim()
        s.text(5, BOB, "yo")
        s.text(100, BOB, "u there")
        s.run_until(320)
        self.assertEqual(s.sends_to(BOB), [(317, "ok bet")])
        self.assertEqual(s.claude_calls[0]["texts"], ["yo", "u there"])

    def test_three_texts_in_a_minute_starts_a_convo(self):
        s = self.sim()
        for t, msg in [(20, "yo"), (25, "wyd"), (30, "u tryna eat")]:
            s.text(t, BOB, msg)
        s.run_until(60)
        # seen at +31; written 10s after their last text (+30) -> +40, sent +41
        self.assertEqual(s.sends_to(BOB), [(41, "ok bet")])
        self.assertEqual(s.claude_calls[0]["texts"], ["yo", "wyd", "u tryna eat"])
        self.assertIn("convo mode on", s.out.getvalue())

    def test_convo_waits_while_they_keep_texting(self):
        s = self.sim()
        for t, msg in [(20, "yo"), (25, "wyd"), (30, "u tryna eat"), (38, "im hungry")]:
            s.text(t, BOB, msg)
        s.run_until(60)
        # +38 arrives after the +31 poll; the re-check at +40 catches it and waits until +48
        self.assertEqual(s.sends_to(BOB), [(49, "ok bet")])
        self.assertEqual(s.claude_calls[0]["texts"], ["yo", "wyd", "u tryna eat", "im hungry"])

    def test_convo_stays_on_for_their_next_text(self):
        s = self.sim()
        for t, msg in [(20, "yo"), (25, "wyd"), (30, "u tryna eat")]:
            s.text(t, BOB, msg)
        s.text(100, BOB, "where at")
        s.run_until(130)
        # one text, but the convo is still going: answered 10s later (not 3-12 min)
        self.assertEqual(s.sends_to(BOB), [(41, "ok bet"), (111, "ok bet")])

    def test_convo_ends_after_five_quiet_minutes(self):
        s = self.sim()
        for t, msg in [(20, "yo"), (25, "wyd"), (30, "u tryna eat")]:
            s.text(t, BOB, msg)
        s.text(400, BOB, "nvm")  # the convo ran out at +340 (5 min after the reply at +40)
        s.run_until(720)
        self.assertEqual(s.sends_to(BOB), [(41, "ok bet"), (702, "ok bet")])  # back to baseline

    def test_convo_reply_isnt_held_forever_if_they_never_pause(self):
        s = self.sim()
        for t in range(20, 400, 5):  # a text every 5s for over 6 minutes
            s.text(t, BOB, f"msg {t}")
        s.run_until(400)
        first_reply = s.sends_to(BOB)[0][0]
        self.assertLessEqual(first_reply, 31 + config.RAPID_MAX_WAIT_SECONDS + 1)

    def test_instant_contacts_get_answered_right_away(self):
        s = self.sim()
        s.text(20, MOM, "are you coming sunday")
        s.text(50, DAD, "call me when you can")
        s.text(80, TIARA, "wyd")
        s.run_until(120)
        self.assertEqual(s.sends_to(MOM), [(32, "ok bet")])
        self.assertEqual(s.sends_to(DAD), [(63, "ok bet")])
        self.assertEqual(s.sends_to(TIARA), [(94, "ok bet")])

    def test_instant_contact_burst_waits_for_them_to_finish(self):
        s = self.sim()
        for t, msg in [(20, "hey"), (22, "are u free"), (29, "call me")]:
            s.text(t, TIARA, msg)
        s.run_until(60)
        self.assertEqual(s.sends_to(TIARA), [(40, "ok bet")])  # 10s after her last text
        self.assertEqual(s.claude_calls[0]["texts"], ["hey", "are u free", "call me"])

    def test_short_sleep_means_a_late_reply(self):
        s = self.sim()
        s.text(20, BOB, "yo")
        s.run_until(100)
        s.sleep_until(1000)  # the reply was due at +331 while the Mac slept
        s.run_until(1010)
        self.assertEqual(s.sends_to(BOB), [(1002, "ok bet")])
        self.assertIn("[wake]", s.out.getvalue())

    def test_long_sleep_leaves_the_text_for_you(self):
        s = self.sim()
        s.text(20, BOB, "yo")
        s.run_until(100)
        s.sleep_until(2000)  # over half an hour later: too late to answer as if nothing happened
        s.run_until(2010)
        self.assertEqual(s.sent, [])
        self.assertEqual(s.claude_calls, [])
        self.assertIn("old by now", s.out.getvalue())

    def test_sleep_in_the_middle_of_a_split_reply_drops_the_rest(self):
        s = self.sim(replies=[{"texts": ["one", "two"], "flag": False, "skip": False}])
        s.text(20, MOM, "hi")
        s.run_until(33)      # "one" went out at +32; "two" is due at +34
        s.sleep_until(500)
        s.text(505, MOM, "hello?")
        s.run_until(520)
        # "two" is dropped, and the bot's own "one" (read only after waking) isn't mistaken for
        # you texting, so her new text still gets an answer
        self.assertEqual(s.sends_to(MOM), [(32, "one"), (517, "ok bet")])

    def test_clock_going_backwards_doesnt_stall_polling(self):
        s = self.sim()
        s.run_until(100)
        s.now = T0 + 40      # the Mac's clock gets set back a minute
        s.fake.add(MOM, "hello?", T0 + 41)
        s.run_until(45)
        self.assertEqual(s.sends_to(MOM), [(42, "ok bet")])


class TestWhoGetsReplies(SimTestCase):
    def test_whitelist_gets_a_notification_not_a_reply(self):
        s = self.sim()
        s.text(20, HAYA, "hey are you around")
        s.run_until(400)
        self.assertEqual(s.sent, [])
        self.assertEqual(s.claude_calls, [])
        self.assertEqual(len(s.notes), 1)
        self.assertIn("Haya Ahmed", s.notes[0][1])

    def test_whitelist_wins_over_instant(self):
        s = self.sim()
        with mock.patch.object(config, "WHITELIST", ["haya", "tiara"]):
            s.text(20, TIARA, "hey")
            s.run_until(60)
        self.assertEqual(s.sent, [])

    def test_no_replies_when_contacts_cant_be_read(self):
        s = self.sim()
        s.contacts_ok = False  # no telling whether this is someone on the whitelist
        s.text(20, BOB, "yo")
        s.text(30, MOM, "hi")
        s.run_until(400)
        self.assertEqual(s.sent, [])
        self.assertEqual(s.claude_calls, [])
        self.assertIn("can't read Contacts", s.out.getvalue())

    def test_whitelisted_number_works_without_contacts(self):
        s = self.sim()
        s.contacts_ok = False
        with mock.patch.object(config, "WHITELIST", ["haya", HAYA]):
            s.text(20, HAYA, "hey")
            s.run_until(60)
        self.assertEqual(s.sent, [])
        self.assertEqual(len(s.notes), 1)

    def test_whitelisted_name_showing_up_later_cancels_a_queued_reply(self):
        s = self.sim(names={})              # Contacts doesn't know Haya's number yet
        s.text(20, HAYA, "hey")             # so a baseline reply gets queued for +331
        s.run_until(100)
        s.names[HAYA] = "Haya Ahmed"        # Contacts gets updated...
        s.text(110, HAYA, "you there?")     # ...and her next text shows who she is
        s.run_until(400)
        self.assertEqual(s.sent, [])
        self.assertEqual(len(s.notes), 1)

    def test_name_is_rechecked_right_before_replying(self):
        s = self.sim(names={})
        s.text(20, HAYA, "hey")             # reply queued for +331
        s.run_until(200)
        s.names[HAYA] = "Haya Ahmed"        # Contacts refreshed, no new text
        s.run_until(400)
        self.assertEqual(s.sent, [])
        self.assertEqual(s.claude_calls, [])

    def test_phone_numbers_work_in_the_lists(self):
        s = self.sim(names={})  # not saved as contacts
        with mock.patch.object(config, "INSTANT_REPLY", ["+1 (909) 555-0001"]):
            s.text(20, BOB, "yo")
            s.run_until(40)
        self.assertEqual(s.sends_to(BOB), [(32, "ok bet")])
        self.assertTrue(s.claude_calls[0]["unknown"])  # Claude is told it's not a saved contact

    def test_tapbacks_group_chats_and_group_events_are_ignored(self):
        s = self.sim()
        s.text(20, BOB, "Loved “yo”", assoc=2000)
        s.text(21, BOB, "yo everyone", chat="chat123456789")
        s.text(22, BOB, "Bob named the conversation", item_type=2)
        s.text(23, BOB, "orphan row", joined=False)  # no chat attached: can't tell where it belongs
        s.run_until(400)
        self.assertEqual(s.claude_calls, [])

    def test_short_codes_are_ignored(self):
        s = self.sim()
        s.text(20, "282828", "Your verification code is 123456")
        s.run_until(400)
        self.assertEqual(s.claude_calls, [])

    def test_texts_from_before_startup_are_ignored(self):
        s = self.sim()
        s.fake.add(BOB, "old text", T0 - 30)
        s.run_until(400)
        self.assertEqual(s.claude_calls, [])

    def test_texts_that_sync_over_too_late_are_left_for_you(self):
        s = self.sim()
        s.events.append((T0 + 700, lambda: s.fake.add(BOB, "from earlier", T0 + 50)))
        s.run_until(1100)
        self.assertEqual(s.claude_calls, [])  # already 11 minutes old when the bot saw it

    def test_photo_counts_toward_a_convo_but_isnt_answered_by_itself(self):
        s = self.sim()
        s.text(20, BOB, "￼", attachments=1)
        s.text(25, BOB, "look at this")
        s.text(30, BOB, "crazy right")
        s.run_until(60)
        self.assertEqual(s.sends_to(BOB), [(41, "ok bet")])  # photo + 2 texts = convo
        self.assertEqual(s.claude_calls[0]["texts"], ["look at this", "crazy right"])
        history_texts = [h["text"] for h in s.claude_calls[0]["history"]]
        self.assertIn("[sent a photo or attachment]", history_texts)

    def test_photo_alone_gets_no_reply(self):
        s = self.sim()
        s.text(20, MOM, None, attachments=1)
        s.run_until(400)
        self.assertEqual(s.claude_calls, [])

    def test_broken_text_encoding_doesnt_stop_polling(self):
        s = self.sim()
        s.events.append((T0 + 20, lambda: s.fake.add(BOB, None, T0 + 20, raw_text=b"\xff\xfe hi")))
        s.text(25, MOM, "hi")
        s.run_until(40)
        self.assertEqual(s.sends_to(MOM), [(32, "ok bet")])


class TestYouAndTheBot(SimTestCase):
    def test_texting_someone_yourself_makes_the_bot_back_off(self):
        s = self.sim()
        s.text(20, BOB, "yo")                  # baseline reply would be due at +331
        s.you_text(50, BOB, "hey whats good")  # you answered him yourself
        s.text(200, BOB, "nm u")               # during the 10 min hands-off window
        s.text(700, BOB, "u still coming")     # after it
        s.run_until(1100)
        self.assertEqual(s.sends_to(BOB), [(1007, "ok bet")])
        self.assertEqual(s.claude_calls[0]["texts"], ["u still coming"])
        self.assertIn("bot stays out of that chat", s.out.getvalue())

    def test_your_text_that_syncs_over_late_still_counts(self):
        s = self.sim()
        s.text(20, BOB, "yo")                              # reply due at +331
        s.you_text(200, BOB, "omw", dated=60)              # sent from your phone at +60, synced at +200
        s.run_until(400)
        self.assertEqual(s.sent, [])

    def test_texting_while_the_mac_sleeps_still_counts(self):
        s = self.sim()
        s.text(20, BOB, "yo")
        s.run_until(100)
        s.fake.add(BOB, "on my way", T0 + 300, from_me=True)  # from your phone while the Mac slept
        s.sleep_until(500)
        s.run_until(520)
        self.assertEqual(s.sent, [])

    def test_restart_remembers_you_were_just_texting_someone(self):
        s = self.sim()
        s.fake.add(MOM, "ill call you in 5", T0 - 60, from_me=True)  # right before the restart
        s.text(20, MOM, "ok")         # bot stays out until 10 min after your text (+540)
        s.text(600, MOM, "hello??")
        s.run_until(620)
        self.assertEqual(s.sends_to(MOM), [(602, "ok bet")])
        self.assertEqual(s.claude_calls[0]["texts"], ["hello??"])

    def test_you_texting_while_claude_writes_cancels_the_reply(self):
        s = self.sim()
        s.on_claude = lambda: s.fake.add(MOM, "hey mom", s.now, from_me=True)
        s.text(20, MOM, "hi")
        s.run_until(60)
        self.assertEqual(s.sent, [])  # the last look before sending sees your text

    def test_same_number_over_imessage_and_sms_is_one_person(self):
        s = self.sim(names={})  # not a saved contact, so this can't lean on matching by name
        s.text(20, BOB, "yo")                               # iMessage chat
        s.you_text(50, BOB, "hey", service="SMS")           # you answer in the SMS chat
        s.run_until(400)
        self.assertEqual(s.sent, [])

    def test_texting_someones_email_covers_their_phone_too(self):
        email = "tiara@icloud.com"
        s = self.sim(names=dict(NAMES, **{email: "Tiara Johnson"}))
        s.you_text(10, email, "hey")      # you text Tiara's iMessage email
        s.text(20, TIARA, "hiii")         # she answers from her phone number
        s.run_until(60)
        self.assertEqual(s.sent, [])

    def test_your_tapback_counts_as_answering(self):
        s = self.sim()
        s.text(20, BOB, "yo")
        s.you_text(50, BOB, "Loved “yo”", assoc=2000)
        s.run_until(400)
        self.assertEqual(s.sent, [])

    def test_bots_own_sends_dont_count_as_you(self):
        s = self.sim()
        s.text(20, MOM, "you eat yet")
        s.text(40, MOM, "theres food here")
        s.run_until(60)
        # the bot's +32 reply shows up in chat.db as a text from you; it must not trigger a takeover
        self.assertEqual(s.sends_to(MOM), [(32, "ok bet"), (48, "ok bet")])

    def test_bots_own_sends_recognized_even_if_unreadable(self):
        s = self.sim()
        s.echo_text = False  # the bot's sends land in chat.db with no readable text
        s.text(20, MOM, "you eat yet")
        s.text(40, MOM, "theres food here")
        s.run_until(60)
        self.assertEqual(s.sends_to(MOM), [(32, "ok bet"), (48, "ok bet")])

    def test_split_reply_goes_out_as_separate_texts(self):
        s = self.sim(replies=[{"texts": ["lmao", "nah im good"], "flag": False, "skip": False}])
        s.text(20, MOM, "you want dessert")
        s.text(50, MOM, "ok")
        s.run_until(70)
        self.assertEqual(s.sends_to(MOM), [(32, "lmao"), (34, "nah im good"), (65, "ok bet")])

    def test_you_texting_mid_split_reply_cancels_the_rest(self):
        s = self.sim(replies=[{"texts": ["one", "two"], "flag": False, "skip": False}])
        s.text(20, MOM, "hi")
        s.you_text(33, MOM, "hey mom")  # you jump in before part 2 (due at +34) goes out
        s.text(70, MOM, "ok")           # and the bot stays out while you're talking
        s.run_until(200)
        self.assertEqual(s.sends_to(MOM), [(32, "one")])

    def test_skip_sends_nothing_and_flag_notifies(self):
        s = self.sim(replies=[{"texts": [], "flag": True, "skip": True}])
        s.text(20, MOM, "grandpa is in the hospital")
        s.run_until(60)
        self.assertEqual(s.sent, [])
        self.assertEqual(len(s.notes), 1)
        self.assertIn("[skip]", s.out.getvalue())

    def test_flagged_reply_still_sends_and_notifies(self):
        s = self.sim(replies=[{"texts": ["maybe, ill lyk"], "flag": True, "skip": False}])
        s.text(20, MOM, "can you drive me to the airport friday")
        s.run_until(60)
        self.assertEqual(s.sends_to(MOM), [(32, "maybe, ill lyk")])
        self.assertEqual(len(s.notes), 1)

    def test_claude_error_doesnt_stop_the_bot(self):
        s = self.sim(replies=[RuntimeError("boom")])
        s.text(20, MOM, "hi")
        s.text(40, MOM, "hello?")
        s.run_until(60)
        self.assertEqual(s.sends_to(MOM), [(47, "ok bet")])
        self.assertIn("boom", s.out.getvalue())

    def test_failed_send_drops_the_rest_of_that_reply(self):
        s = self.sim(replies=[{"texts": ["one", "two"], "flag": False, "skip": False}])
        s.send_ok = False
        s.text(20, MOM, "hi")
        s.run_until(60)
        self.assertEqual(s.sent, [])
        self.assertEqual(s.bot.outbox, [])

    def test_history_includes_both_sides_oldest_first(self):
        s = self.sim()
        s.fake.add(BOB, "you coming tmrw?", T0 - 1000)
        s.fake.add(BOB, "prob", T0 - 990, from_me=True)
        s.fake.add(BOB, "Loved “prob”", T0 - 985, assoc=2000)  # tapback: left out
        s.fake.add(BOB, "sms era", T0 - 980, service="SMS")             # same person, SMS chat
        s.text(20, BOB, "yo")
        s.run_until(340)  # seen at +31, baseline reply at +331
        history = s.claude_calls[0]["history"]
        self.assertEqual([(h["from_me"], h["text"]) for h in history],
                         [(False, "you coming tmrw?"), (True, "prob"), (False, "sms era"),
                          (False, "yo")])

    def test_end_to_end_with_the_real_reply_code(self):
        """The Bot plus the real claude_api (prompt, parsing, cleanup), with only the HTTP faked."""
        s = self.sim()
        s._patches[2].stop()  # use the real generate_reply
        s._patches.pop(2)
        fake_client = FakeClient(['{"messages": ["Lmao yeah.", "Im down"], "flag": false, "skip": false}'])
        with mock.patch("claude_api._get_client", return_value=fake_client), \
                mock.patch.object(claude_api, "_use_schema", True):
            s.text(20, MOM, "wanna get boba later")
            s.run_until(40)
        self.assertEqual(s.sends_to(MOM), [(32, "lmao yeah"), (34, "im down")])
        prompt = fake_client.messages.calls[0]["messages"][0]["content"]
        self.assertIn("you're texting with: Mom", prompt)
        self.assertIn("wanna get boba later", prompt)


class FakeMessages:
    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        behavior = self.behaviors.pop(0)
        if isinstance(behavior, Exception):
            raise behavior
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=behavior)])


class FakeClient:
    def __init__(self, behaviors, retrieve=None):
        self.messages = FakeMessages(behaviors)
        self.models = SimpleNamespace(retrieve=retrieve or (lambda model: SimpleNamespace(id=model)))


def http_error(cls, status):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return cls(message=f"error {status}", response=httpx.Response(status, request=request), body=None)


class TestClaudeApi(unittest.TestCase):
    def setUp(self):
        claude_api._use_schema = True

    def tearDown(self):
        claude_api._use_schema = True

    def test_parse_reply_shapes(self):
        p = claude_api._parse
        self.assertEqual(p('{"messages": ["Lol.", "nah"], "flag": false, "skip": false}'),
                         {"texts": ["lol", "nah"], "flag": False, "skip": False})
        self.assertEqual(p('{"reply": "probably, ill check", "flag": true}'),
                         {"texts": ["probably, ill check"], "flag": True, "skip": False})
        self.assertEqual(p('```json\n{"messages": ["yeah"], "flag": false, "skip": false}\n```')["texts"],
                         ["yeah"])
        self.assertEqual(p('sure: {"messages": ["ok"], "flag": "true", "skip": "false"} done'),
                         {"texts": ["ok"], "flag": True, "skip": False})
        self.assertIsNone(p("not json at all"))
        self.assertIsNone(p('{"messages": ["unterminated"'))

    def test_parse_skip_and_limits(self):
        p = claude_api._parse
        self.assertEqual(p('{"messages": [], "flag": true, "skip": true}'),
                         {"texts": [], "flag": True, "skip": True})
        self.assertEqual(p('{"messages": ["ok"], "flag": false, "skip": true}')["texts"], [])
        self.assertTrue(p('{"messages": [""], "flag": false, "skip": false}')["skip"])
        self.assertEqual(len(p('{"messages": ["a", "b", "c", "d"], "flag": false, "skip": false}')["texts"]),
                         config.MAX_REPLY_TEXTS)

    def test_parse_drops_repeated_texts(self):
        p = claude_api._parse
        self.assertEqual(p('{"messages": ["lol", "LOL.", "ok"], "flag": false, "skip": false}')["texts"],
                         ["lol", "ok"])
        self.assertEqual(p('{"messages": ["lol", "", "lol"], "flag": false, "skip": false}')["texts"],
                         ["lol"])

    def test_humanize(self):
        h = claude_api.humanize
        self.assertEqual(h("Yeah I'm at work rn."), "yeah i'm at work rn")
        self.assertEqual(h("ok..."), "ok...")
        self.assertEqual(h("yeah — at work"), "yeah, at work")
        self.assertEqual(h("“lol ok”"), "lol ok")
        self.assertEqual(h("Ryan: LOL"), "lol")
        self.assertEqual(h("look https://Example.com/AbC"), "look https://Example.com/AbC")
        self.assertEqual(h("  "), "")

    def test_prompt_has_time_people_history_and_new_texts(self):
        old_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/Los_Angeles"
        time.tzset()
        try:
            when = time.mktime((2026, 10, 3, 15, 42, 0, 0, 0, -1))  # sat oct 3 2026, 3:42 pm pacific
            history = [{"time": when - 600, "from_me": False, "text": "you coming tmrw?"},
                       {"time": when - 540, "from_me": True, "text": "prob"}]
            prompt = claude_api.build_prompt("Bob Smith", history, ["yo", "wyd"], when)
            self.assertIn("right now it's saturday, oct 3, 3:42 pm.", prompt)
            self.assertIn("you're texting with: Bob Smith\n", prompt)
            self.assertIn("[sat 3:32 pm] Bob Smith: you coming tmrw?", prompt)
            self.assertIn("[sat 3:33 pm] ryan: prob", prompt)
            self.assertTrue(prompt.endswith("not answered yet:\nyo\nwyd"))
            self.assertIn("(not in ryan's contacts)",
                          claude_api.build_prompt("+19095550001", [], ["hi"], when, unknown=True))
        finally:
            if old_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old_tz
            time.tzset()

    def test_request_uses_structured_output(self):
        client = FakeClient(['{"messages": ["yeah"], "flag": false, "skip": false}'])
        with mock.patch("claude_api._get_client", return_value=client):
            result = claude_api.generate_reply("Bob", [], ["yo"], T0)
        self.assertEqual(result["texts"], ["yeah"])
        call = client.messages.calls[0]
        self.assertEqual(call["model"], config.MODEL)
        self.assertEqual(call["system"], claude_api.SYSTEM_PROMPT)
        self.assertEqual(call["output_config"]["format"],
                         {"type": "json_schema", "schema": claude_api._REPLY_SCHEMA})

    def test_falls_back_to_plain_json_if_schema_rejected(self):
        client = FakeClient([http_error(anthropic.BadRequestError, 400),
                             '{"messages": ["yeah"], "flag": false, "skip": false}',
                             '{"messages": ["ok"], "flag": false, "skip": false}'])
        with mock.patch("claude_api._get_client", return_value=client), redirect_stdout(io.StringIO()):
            self.assertEqual(claude_api.generate_reply("Bob", [], ["yo"], T0)["texts"], ["yeah"])
            self.assertEqual(claude_api.generate_reply("Bob", [], ["yo"], T0)["texts"], ["ok"])
        self.assertNotIn("output_config", client.messages.calls[1])
        self.assertNotIn("output_config", client.messages.calls[2])
        self.assertFalse(claude_api._use_schema)

    def test_falls_back_on_an_sdk_too_old_for_output_config(self):
        client = FakeClient([TypeError("create() got an unexpected keyword argument 'output_config'"),
                             '{"messages": ["yeah"], "flag": false, "skip": false}'])
        with mock.patch("claude_api._get_client", return_value=client), redirect_stdout(io.StringIO()):
            self.assertEqual(claude_api.generate_reply("Bob", [], ["yo"], T0)["texts"], ["yeah"])
        self.assertFalse(claude_api._use_schema)

    def test_other_bad_requests_dont_turn_off_the_schema(self):
        client = FakeClient([http_error(anthropic.BadRequestError, 400),
                             http_error(anthropic.BadRequestError, 400)])
        with mock.patch("claude_api._get_client", return_value=client):
            with self.assertRaises(anthropic.BadRequestError):
                claude_api.generate_reply("Bob", [], ["yo"], T0)
        self.assertTrue(claude_api._use_schema)

    def test_check_api_key(self):
        def raises(exc):
            def retrieve(model):
                raise exc
            return retrieve
        ok = FakeClient([])
        bad_key = FakeClient([], retrieve=raises(http_error(anthropic.AuthenticationError, 401)))
        no_model = FakeClient([], retrieve=raises(http_error(anthropic.NotFoundError, 404)))
        offline = FakeClient([], retrieve=raises(anthropic.APIConnectionError(
            request=httpx.Request("GET", "https://api.anthropic.com/v1/models/x"))))
        with mock.patch("claude_api._get_client", return_value=ok):
            self.assertIsNone(claude_api.check_api_key())
        with mock.patch("claude_api._get_client", return_value=bad_key):
            self.assertIn("rejected", claude_api.check_api_key())
        with mock.patch("claude_api._get_client", return_value=no_model):
            self.assertIn(config.MODEL, claude_api.check_api_key())
        with mock.patch("claude_api._get_client", return_value=offline), redirect_stdout(io.StringIO()):
            self.assertIsNone(claude_api.check_api_key())  # can't check right now: don't block startup


class TestRealSdk(unittest.TestCase):
    """The installed anthropic SDK itself, talking to a mocked HTTP endpoint (no network)."""

    def setUp(self):
        claude_api._use_schema = True
        self.requests = []

    def tearDown(self):
        claude_api._use_schema = True

    def client(self, handler):
        def record(request):
            self.requests.append(request)
            return handler(request)
        return anthropic.Anthropic(api_key="test-key", max_retries=0,
                                   http_client=httpx.Client(transport=httpx.MockTransport(record)))

    @staticmethod
    def message_response(text):
        return httpx.Response(200, json={
            "id": "msg_test", "type": "message", "role": "assistant", "model": config.MODEL,
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 5}})

    def test_sdk_sends_output_config_and_we_read_the_reply(self):
        client = self.client(lambda r: self.message_response(
            '{"messages": ["lol yeah"], "flag": false, "skip": false}'))
        with mock.patch("claude_api._get_client", return_value=client):
            result = claude_api.generate_reply("Bob", [], ["yo"], T0)
        self.assertEqual(result, {"texts": ["lol yeah"], "flag": False, "skip": False})
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["model"], config.MODEL)
        self.assertEqual(body["output_config"]["format"]["type"], "json_schema")
        self.assertEqual(body["output_config"]["format"]["schema"], claude_api._REPLY_SCHEMA)

    def test_sdk_fallback_when_output_config_is_rejected(self):
        def handler(request):
            if "output_config" in json.loads(request.content):
                return httpx.Response(400, json={"type": "error", "error": {
                    "type": "invalid_request_error", "message": "output_config: not supported"}})
            return self.message_response('{"messages": ["ok"], "flag": false, "skip": false}')
        with mock.patch("claude_api._get_client", return_value=self.client(handler)), \
                redirect_stdout(io.StringIO()):
            result = claude_api.generate_reply("Bob", [], ["yo"], T0)
        self.assertEqual(result["texts"], ["ok"])
        self.assertFalse(claude_api._use_schema)

    def test_sdk_bad_key_is_caught_at_startup(self):
        client = self.client(lambda r: httpx.Response(401, json={"type": "error", "error": {
            "type": "authentication_error", "message": "invalid x-api-key"}}))
        with mock.patch("claude_api._get_client", return_value=client):
            self.assertIn("rejected", claude_api.check_api_key())
        self.assertIn(config.MODEL, str(self.requests[0].url))

    def test_sdk_good_key_passes_startup_check(self):
        client = self.client(lambda r: httpx.Response(200, json={
            "id": config.MODEL, "type": "model", "display_name": "Claude Haiku 4.5",
            "created_at": "2025-10-01T00:00:00Z"}))
        with mock.patch("claude_api._get_client", return_value=client):
            self.assertIsNone(claude_api.check_api_key())


class TestMatching(unittest.TestCase):
    def test_instant_is_whole_word(self):
        self.assertTrue(main.is_instant("Mom"))
        self.assertTrue(main.is_instant("Mom ❤️"))
        self.assertTrue(main.is_instant("Tiara Johnson"))
        self.assertTrue(main.is_instant("Dad (cell)"))
        self.assertFalse(main.is_instant("Momo"))
        self.assertFalse(main.is_instant("Ali Haddad"))

    def test_whitelist_is_partial(self):
        self.assertTrue(main.is_whitelisted("Haya Ahmed"))
        self.assertTrue(main.is_whitelisted("MARCUS"))
        self.assertTrue(main.is_whitelisted("Mom / Haya Ahmed"))  # a number two contacts share
        self.assertFalse(main.is_whitelisted("Dad"))  # moved to INSTANT_REPLY
        self.assertFalse(main.is_whitelisted(None, "+19095550009"))

    def test_numbers_and_emails(self):
        with mock.patch.object(config, "WHITELIST", ["(909) 555-0001", "Pal@iCloud.com"]):
            self.assertTrue(main.is_whitelisted("+19095550001", "+19095550001"))
            self.assertTrue(main.is_whitelisted(None, "+19095550001"))
            self.assertTrue(main.is_whitelisted("someone", "pal@icloud.com"))
            self.assertFalse(main.is_whitelisted("someone", "+19095550002"))

    def test_automated_senders(self):
        self.assertTrue(main.is_automated("282828"))
        self.assertTrue(main.is_automated("urn:biz:1234-5678"))
        self.assertFalse(main.is_automated("+19095550001"))
        self.assertFalse(main.is_automated("friend@icloud.com"))


class TestTimingHelpers(unittest.TestCase):
    def test_rapid_fire_uses_text_timestamps(self):
        self.assertTrue(timing.is_rapid_fire([0, 10, 20]))
        self.assertTrue(timing.is_rapid_fire([0, 50, 60]))
        self.assertFalse(timing.is_rapid_fire([0, 30, 61]))
        self.assertFalse(timing.is_rapid_fire([0, 10]))
        self.assertTrue(timing.is_rapid_fire([0, 500, 510, 520]))  # only the latest 3 matter

    def test_typing_delay_bounds(self):
        for _ in range(200):
            d = timing.typing_delay("nah im good")
            self.assertGreater(d, 0.8 * config.TYPING_BASE_SECONDS)
            self.assertLessEqual(d, config.TYPING_MAX_SECONDS)
        self.assertEqual(timing.typing_delay("x" * 500), config.TYPING_MAX_SECONDS)


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestContacts(unittest.TestCase):
    def setUp(self):
        contacts._book = None
        contacts._loaded_at = 0.0
        contacts._failed_at = None
        self.now = 1_000_000.0
        clock = mock.patch.object(contacts, "time", SimpleNamespace(time=lambda: self.now))
        clock.start()
        self.addCleanup(clock.stop)
        self.addCleanup(setattr, contacts, "_book", None)

    def test_normalize(self):
        self.assertEqual(contacts.normalize("+1 (909) 555-1234"), "9095551234")
        self.assertEqual(contacts.normalize("+19095551234"), "9095551234")
        self.assertEqual(contacts.normalize(" Foo@Bar.com "), "foo@bar.com")
        self.assertEqual(contacts.normalize("282828"), "282828")
        self.assertIsNone(contacts.normalize(None))

    def test_formatted_numbers_match(self):
        book = "Mom\t(909) 555-1234\nTiara Johnson\tTiara@iCloud.com\nmissing value\t555-0000\n"
        with mock.patch("contacts.subprocess.run", return_value=completed(book)) as run:
            self.assertEqual(contacts.resolve("+19095551234"), "Mom")
            self.assertEqual(contacts.resolve("tiara@icloud.com"), "Tiara Johnson")
            self.assertEqual(contacts.resolve("+18005550000"), "+18005550000")  # not a contact
        self.assertEqual(run.call_count, 1)  # one read of the whole address book
        self.assertEqual(contacts.entry_count(), 2)

    def test_shared_number_keeps_every_name(self):
        book = "Mom\t(909) 555-1234\nHaya Ahmed\t909-555-1234\n"
        with mock.patch("contacts.subprocess.run", return_value=completed(book)):
            self.assertEqual(contacts.resolve("+19095551234"), "Mom / Haya Ahmed")

    def test_unreadable_contacts_means_unknown(self):
        with mock.patch("contacts.subprocess.run",
                        return_value=completed(returncode=1, stderr="Not authorized")), \
                redirect_stdout(io.StringIO()):
            self.assertIn("Not authorized", contacts.load())
            self.assertIsNone(contacts.resolve("+19095551234"))

    def test_empty_read_counts_as_a_failure(self):
        with mock.patch("contacts.subprocess.run", return_value=completed("garbage\n")):
            self.assertIsNotNone(contacts.load())
        self.assertIsNone(contacts._book)

    def test_failed_read_is_retried_later(self):
        def slow(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="osascript", timeout=60)
        with mock.patch("contacts.subprocess.run", side_effect=slow) as run, \
                redirect_stdout(io.StringIO()):
            self.assertIsNone(contacts.resolve("+19095551234"))
            self.assertIsNone(contacts.resolve("+19095551234"))
            self.assertEqual(run.call_count, 1)  # not hammering Contacts right away
        self.now += contacts.RETRY_SECONDS + 1
        with mock.patch("contacts.subprocess.run", return_value=completed("Mom\t909-555-1234\n")):
            self.assertEqual(contacts.resolve("+19095551234"), "Mom")

    def test_address_book_refreshes(self):
        with mock.patch("contacts.subprocess.run", return_value=completed("Mom\t9095551234\n")):
            self.assertEqual(contacts.resolve("+12135550000"), "+12135550000")
        self.now += contacts.BOOK_REFRESH_SECONDS + 1
        with mock.patch("contacts.subprocess.run",
                        return_value=completed("Mom\t9095551234\nNew Friend\t(213) 555-0000\n")):
            self.assertEqual(contacts.resolve("+12135550000"), "New Friend")

    def test_old_book_is_kept_if_a_refresh_fails(self):
        with mock.patch("contacts.subprocess.run", return_value=completed("Mom\t9095551234\n")):
            self.assertEqual(contacts.resolve("+19095551234"), "Mom")
        self.now += contacts.BOOK_REFRESH_SECONDS + 1
        with mock.patch("contacts.subprocess.run", return_value=completed(returncode=1)), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(contacts.resolve("+19095551234"), "Mom")


class TestMessenger(unittest.TestCase):
    def test_imessage_first(self):
        with mock.patch("messenger.subprocess.run", return_value=completed()) as run:
            self.assertTrue(messenger.send(MOM, "hi", "iMessage"))
        self.assertEqual(run.call_count, 1)
        self.assertIn("service type = iMessage", run.call_args[0][0][2])
        self.assertEqual(run.call_args[0][0][3:], [MOM, "hi"])

    def test_falls_back_to_sms(self):
        results = [completed(returncode=1, stderr="no iMessage"), completed()]
        with mock.patch("messenger.subprocess.run", side_effect=results) as run:
            self.assertTrue(messenger.send(DAD, "hi", "iMessage"))
        self.assertIn("service type = SMS", run.call_args_list[1][0][0][2])

    def test_sms_chats_try_sms_first(self):
        with mock.patch("messenger.subprocess.run", return_value=completed()) as run:
            self.assertTrue(messenger.send(DAD, "hi", "RCS"))
        self.assertIn("service type = SMS", run.call_args[0][0][2])

    def test_timeout_doesnt_risk_a_double_send(self):
        with mock.patch("messenger.subprocess.run",
                        side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=30)) as run, \
                redirect_stdout(io.StringIO()):
            self.assertFalse(messenger.send(MOM, "hi"))
        self.assertEqual(run.call_count, 1)

    def test_both_fail(self):
        with mock.patch("messenger.subprocess.run", return_value=completed(returncode=1)), \
                redirect_stdout(io.StringIO()):
            self.assertFalse(messenger.send(MOM, "hi"))


class TestChatDB(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def open(self, old_schema=False):
        fake = FakeChatDB(os.path.join(self._tmp.name, "chat.db"), old_schema=old_schema)
        chat_db = dbmod.ChatDB(fake.path)
        chat_db.connect()
        self.addCleanup(chat_db.close)
        self.addCleanup(fake.conn.close)
        return fake, chat_db

    def test_message_text(self):
        def row(text, body=None):
            return {"text": text, "attributed_body": body}
        self.assertEqual(dbmod.message_text(row("hey")), "hey")
        self.assertIsNone(dbmod.message_text(row("￼")))
        self.assertEqual(dbmod.message_text(row("￼check this")), "check this")
        body = b"\x04\x0bstreamtyped NSString\x01\x94\x84\x01+\x05hello\x86"
        self.assertEqual(dbmod.message_text(row(None, body)), "hello")

    def test_new_messages_by_rowid(self):
        fake, chat_db = self.open()
        before = chat_db.max_rowid()
        fake.add(BOB, "yo", T0)
        fake.add(BOB, "Loved “yo”", T0 + 1, assoc=2000)            # their tapback: dropped
        fake.add(BOB, "Liked “wyd”", T0 + 2, assoc=2001, from_me=True)  # yours: kept
        fake.add(BOB, "Bob left", T0 + 3, item_type=3)
        fake.add(BOB, "wyd", T0 + 4, service="SMS", attachments=1)
        rows = chat_db.new_messages(before, chat_db.max_rowid())
        self.assertEqual([r["text"] for r in rows], ["yo", "Liked “wyd”", "wyd"])
        self.assertEqual(rows[1]["associated_message_type"], 2001)
        self.assertEqual(rows[2]["service"], "SMS")
        self.assertEqual(rows[2]["cache_has_attachments"], 1)
        self.assertEqual(rows[0]["handle_id"], BOB)
        self.assertEqual(rows[0]["chat_identifier"], BOB)
        self.assertEqual(chat_db.new_messages(chat_db.max_rowid(), chat_db.max_rowid()), [])

    def test_late_synced_rows_are_still_new(self):
        fake, chat_db = self.open()
        cursor = chat_db.max_rowid()
        fake.add(BOB, "sent hours ago, synced just now", T0 - 7200, from_me=True)
        self.assertEqual(len(chat_db.new_messages(cursor, chat_db.max_rowid())), 1)

    def test_broken_utf8_is_readable(self):
        fake, chat_db = self.open()
        fake.add(BOB, None, T0, raw_text=b"\xff\xfe hi")
        rows = chat_db.new_messages(0, chat_db.max_rowid())
        self.assertIn("hi", rows[0]["text"])

    def test_recent_messages_for_startup(self):
        fake, chat_db = self.open()
        fake.add(MOM, "old", T0 - 3600, from_me=True)
        fake.add(MOM, "recent", T0 - 60, from_me=True)
        self.assertEqual([r["text"] for r in chat_db.recent_messages(T0 - 600)], ["recent"])

    def test_older_chat_db_without_optional_columns(self):
        fake, chat_db = self.open(old_schema=True)
        fake.add(BOB, "yo", T0)
        rows = chat_db.new_messages(0, chat_db.max_rowid())
        self.assertEqual(rows[0]["text"], "yo")
        self.assertIsNone(rows[0]["service"])
        self.assertIsNone(rows[0]["associated_message_type"])
        self.assertEqual(len(chat_db.chat_history(BOB, 5)), 1)

    def test_chat_history_covers_every_chat_with_that_handle(self):
        fake, chat_db = self.open()
        for i in range(30):
            fake.add(BOB, f"msg {i}", T0 + i, from_me=(i % 2 == 1),
                     service="SMS" if i % 3 == 0 else "iMessage")
        fake.add(MOM, "other person", T0 + 100)
        rows = chat_db.chat_history(BOB, 20)
        self.assertEqual([r["text"] for r in rows], [f"msg {i}" for i in range(10, 30)])
        self.assertEqual(chat_db.chat_history(None, 5), [])


if __name__ == "__main__":
    unittest.main()
