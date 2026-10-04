"""Resolve iMessage handles (phone / email) to contact display names via the Contacts app."""
import re
import subprocess
import time

# Reads every contact's name, phones and emails in one go (bulk property reads are a few
# Apple events, so this is fast even for a big address book). Matching then happens in
# Python on normalized digits, so a contact saved as "(909) 555-1234" matches the
# handle "+19095551234".
_DUMP_SCRIPT = """
on run
    tell application "Contacts"
        set nameList to name of every person
        set phoneLists to value of phones of every person
        set emailLists to value of emails of every person
    end tell
    set outLines to {}
    repeat with i from 1 to count of nameList
        set personName to item i of nameList
        if personName is not missing value then
            repeat with v in item i of phoneLists
                set end of outLines to (personName as text) & tab & (v as text)
            end repeat
            repeat with v in item i of emailLists
                set end of outLines to (personName as text) & tab & (v as text)
            end repeat
        end if
    end repeat
    set AppleScript's text item delimiters to linefeed
    set outText to outLines as text
    set AppleScript's text item delimiters to ""
    return outText
end run
"""

# Fallback if the bulk read fails: search contacts one handle at a time (slow).
_SEARCH_SCRIPT = """
on run {q}
    set matchName to ""
    tell application "Contacts"
        repeat with p in people
            try
                repeat with ph in (phones of p)
                    if (value of ph as text) contains q then
                        set matchName to (name of p)
                        exit repeat
                    end if
                end repeat
            end try
            if matchName is not "" then exit repeat
            try
                repeat with em in (emails of p)
                    if (value of em as text) contains q then
                        set matchName to (name of p)
                        exit repeat
                    end if
                end repeat
            end try
            if matchName is not "" then exit repeat
        end repeat
    end tell
    return matchName
end run
"""

BOOK_REFRESH_SECONDS = 3600  # re-read Contacts hourly so newly added contacts get picked up
RETRY_SECONDS = 300          # after a failed lookup, wait this long before asking Contacts again

_book = None            # normalized handle -> contact name
_book_loaded_at = 0.0
_book_failed_at = None
_cache = {}             # handle -> resolved name (or the handle itself if it isn't a contact)
_search_failed = {}     # handle -> when the one-off search last failed


def normalize(handle):
    """Phone numbers -> last 10 digits, emails -> lowercase, so different formats compare equal."""
    if not handle:
        return None
    handle = handle.strip()
    if "@" in handle:
        return handle.lower()
    digits = re.sub(r"\D", "", handle)
    if len(digits) >= 10:
        return digits[-10:]
    return digits or None


def _load_book(now):
    global _book, _book_loaded_at, _book_failed_at
    try:
        out = subprocess.run(
            ["osascript", "-e", _DUMP_SCRIPT],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        _book_failed_at = now
        print(f"[contacts] couldn't read Contacts: {e}")
        return
    if out.returncode != 0:
        _book_failed_at = now
        print(f"[contacts] couldn't read Contacts: {out.stderr.strip()}")
        return
    book = {}
    for line in out.stdout.splitlines():
        name, _, value = line.partition("\t")
        name = name.strip()
        key = normalize(value)
        if name and name != "missing value" and key:
            book.setdefault(key, name)
    _book = book
    _book_loaded_at = now
    _book_failed_at = None
    _cache.clear()


def _search(handle, now):
    """The old one-at-a-time AppleScript search. Failures (e.g. a timeout) aren't cached."""
    if now - _search_failed.get(handle, float("-inf")) < RETRY_SECONDS:
        return handle
    digits = re.sub(r"\D", "", handle)
    query = handle.strip() if "@" in handle else (digits[-10:] if len(digits) >= 10 else digits)
    if not query:
        return handle
    try:
        out = subprocess.run(
            ["osascript", "-e", _SEARCH_SCRIPT, query],
            capture_output=True, text=True, timeout=20,
        )
    except Exception:
        _search_failed[handle] = now
        return handle
    if out.returncode != 0:
        _search_failed[handle] = now
        return handle
    result = out.stdout.strip() or handle
    _cache[handle] = result
    return result


def resolve(handle):
    """Return a display name for a handle, or the handle itself if it isn't a contact."""
    if not handle:
        return "unknown"
    now = time.time()
    stale = _book is None or now - _book_loaded_at > BOOK_REFRESH_SECONDS
    if stale and (_book_failed_at is None or now - _book_failed_at >= RETRY_SECONDS):
        _load_book(now)
    if handle in _cache:
        return _cache[handle]
    name = _book.get(normalize(handle)) if _book is not None else None
    if name:
        _cache[handle] = name
        return name
    # Not in the address book we read (or we couldn't read it): try the slower one-off search,
    # so a bad bulk read can never leave the whitelist matching nobody.
    return _search(handle, now)
