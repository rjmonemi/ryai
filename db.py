"""Read-only access to the macOS iMessage database (chat.db)."""
import sqlite3

import config

# Apple epoch (2001-01-01) offset from the Unix epoch (1970-01-01), in seconds.
APPLE_EPOCH_OFFSET = 978307200


def apple_to_unix(date_value):
    """Convert a chat.db `date` (Apple epoch; ns on modern macOS, s on older) to Unix seconds."""
    if date_value is None:
        return 0.0
    # Modern macOS stores nanoseconds; older versions stored seconds. Detect by magnitude.
    if abs(date_value) > 1e12:
        seconds = date_value / 1e9
    else:
        seconds = float(date_value)
    return seconds + APPLE_EPOCH_OFFSET


def unix_to_apple_ns(unix_seconds):
    """Convert Unix seconds to a chat.db `date` threshold in nanoseconds."""
    return int((unix_seconds - APPLE_EPOCH_OFFSET) * 1e9)


class ChatDB:
    def __init__(self, path=None):
        self.path = path or config.CHAT_DB_PATH
        self.conn = None
        self.columns = set()  # message-table columns this macOS version has

    def connect(self):
        """Open chat.db read-only. Raises sqlite3.OperationalError on permission/locate errors."""
        uri = f"file:{self.path}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row
        # One message with broken UTF-8 shouldn't make every query that touches it fail.
        self.conn.text_factory = lambda b: b.decode("utf-8", errors="replace")
        self.columns = {r["name"] for r in self.conn.execute("PRAGMA table_info(message)")}

    def _optional(self, column):
        """Select an optional message column, or NULL if this chat.db doesn't have it."""
        if column in self.columns:
            return f"m.{column} AS {column}"
        return f"NULL AS {column}"

    def _real_texts_only(self, keep_my_reactions=False):
        """SQL that drops tapbacks/reactions and group events (renames, joins): not real texts.

        With keep_my_reactions, your own tapbacks stay in: reacting to a text counts as answering it.
        """
        sql = ""
        if "associated_message_type" in self.columns:
            mine = "m.is_from_me = 1 OR " if keep_my_reactions else ""
            sql += f" AND ({mine}COALESCE(m.associated_message_type, 0) = 0)"
        if "item_type" in self.columns:
            sql += " AND COALESCE(m.item_type, 0) = 0"
        return sql

    def _select_messages(self):
        return f"""
            SELECT
                m.ROWID            AS msg_id,
                m.text             AS text,
                m.attributedBody   AS attributed_body,
                m.is_from_me       AS is_from_me,
                m.date             AS date,
                {self._optional("service")},
                {self._optional("cache_has_attachments")},
                {self._optional("associated_message_type")},
                h.id               AS handle_id,
                c.ROWID            AS chat_id,
                c.chat_identifier  AS chat_identifier
            FROM message m
            LEFT JOIN handle h            ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join j ON j.message_id = m.ROWID
            LEFT JOIN chat c              ON c.ROWID = j.chat_id
        """

    def max_rowid(self):
        """The newest message row's ROWID (0 if there are none)."""
        return self.conn.execute("SELECT MAX(ROWID) FROM message").fetchone()[0] or 0

    def new_messages(self, after_rowid, up_to_rowid):
        """Rows added since the last poll (after_rowid < ROWID <= up_to_rowid), oldest first.

        Going by ROWID instead of a time window means nothing is missed when the Mac sleeps
        or a text syncs over from your phone late.
        """
        sql = self._select_messages() + f"""
            WHERE m.ROWID > ? AND m.ROWID <= ?{self._real_texts_only(keep_my_reactions=True)}
            ORDER BY m.date ASC, m.ROWID ASC
        """
        return self.conn.execute(sql, (after_rowid, up_to_rowid)).fetchall()

    def recent_messages(self, since_unix):
        """Rows with date > since_unix, oldest first (used at startup)."""
        threshold = unix_to_apple_ns(since_unix)
        sql = self._select_messages() + f"""
            WHERE m.date > ?{self._real_texts_only(keep_my_reactions=True)}
            ORDER BY m.date ASC, m.ROWID ASC
        """
        return self.conn.execute(sql, (threshold,)).fetchall()

    def chat_history(self, chat_identifier, limit):
        """The last `limit` real messages with this person, both sides, oldest first.

        Goes by chat_identifier (their handle), so an iMessage chat and an SMS chat with
        the same number are read together.
        """
        if not chat_identifier:
            return []
        sql = f"""
            SELECT
                m.ROWID            AS msg_id,
                m.text             AS text,
                m.attributedBody   AS attributed_body,
                m.is_from_me       AS is_from_me,
                m.date             AS date,
                {self._optional("cache_has_attachments")}
            FROM message m
            JOIN chat_message_join j ON j.message_id = m.ROWID
            JOIN chat c              ON c.ROWID = j.chat_id
            WHERE c.chat_identifier = ?{self._real_texts_only()}
            ORDER BY m.date DESC, m.ROWID DESC
            LIMIT ?
        """
        rows = self.conn.execute(sql, (chat_identifier, limit)).fetchall()
        return list(reversed(rows))

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None


def decode_attributed_body(blob):
    """Best-effort extraction of message text from attributedBody.

    On modern macOS many messages have m.text = NULL and the body lives in
    attributedBody as an archived (streamtyped) NSAttributedString. This is a
    heuristic parse of that blob — good enough for the vast majority of texts.
    """
    if not blob:
        return None
    try:
        data = bytes(blob)
    except Exception:
        return None
    idx = data.find(b"NSString")
    if idx == -1:
        return None
    p = idx + len(b"NSString")
    plus = data.find(b"\x2b", p)        # a '+' byte precedes the length
    if plus == -1:
        return None
    p = plus + 1
    if p >= len(data):
        return None
    length = data[p]
    p += 1
    if length == 0x81:                   # next 2 bytes (little-endian) hold the length
        length = int.from_bytes(data[p:p + 2], "little")
        p += 2
    elif length == 0x82:                 # next 4 bytes (little-endian) hold the length
        length = int.from_bytes(data[p:p + 4], "little")
        p += 4
    text = data[p:p + length].decode("utf-8", errors="replace").strip()
    return text or None


def message_text(row):
    """Return the best available text for a message row (text column, else attributedBody).

    Attachments appear in the text as U+FFFC placeholder characters; those are stripped,
    so a photo with no caption returns None.
    """
    text = row["text"] or decode_attributed_body(row["attributed_body"])
    if not text:
        return None
    text = text.replace("￼", "").strip()
    return text or None
