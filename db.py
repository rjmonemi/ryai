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

    def connect(self):
        """Open chat.db read-only. Raises sqlite3.OperationalError on permission/locate errors."""
        uri = f"file:{self.path}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row

    def recent_messages(self, since_unix):
        """Return message rows with date > since_unix, oldest first."""
        threshold = unix_to_apple_ns(since_unix)
        sql = """
            SELECT
                m.ROWID            AS msg_id,
                m.text             AS text,
                m.attributedBody   AS attributed_body,
                m.is_from_me       AS is_from_me,
                m.date             AS date,
                h.id               AS handle_id,
                c.ROWID            AS chat_id,
                c.chat_identifier  AS chat_identifier
            FROM message m
            LEFT JOIN handle h            ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join j ON j.message_id = m.ROWID
            LEFT JOIN chat c              ON c.ROWID = j.chat_id
            WHERE m.date > ?
            ORDER BY m.date ASC
        """
        return self.conn.execute(sql, (threshold,)).fetchall()

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
    """Return the best available text for a message row (text column, else attributedBody)."""
    if row["text"]:
        return row["text"]
    return decode_attributed_body(row["attributed_body"])
