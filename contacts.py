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
    if (count of phoneLists) is not (count of nameList) or (count of emailLists) is not (count of nameList) then
        error "Contacts changed while being read; try again"
    end if
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

BOOK_REFRESH_SECONDS = 3600  # re-read Contacts hourly so newly added contacts get picked up
RETRY_SECONDS = 300          # after a failed read, wait this long before trying again

_book = None          # normalized handle -> contact name(s)
_loaded_at = 0.0
_failed_at = None


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


def load():
    """Read the whole address book now. Returns None on success, or what went wrong."""
    global _book, _loaded_at, _failed_at
    now = time.time()
    try:
        out = subprocess.run(
            ["osascript", "-e", _DUMP_SCRIPT],
            capture_output=True, text=True, timeout=60,
        )
    except Exception as e:
        _failed_at = now
        return f"couldn't read Contacts ({e})"
    if out.returncode != 0:
        _failed_at = now
        return f"couldn't read Contacts ({out.stderr.strip() or 'osascript failed'})"
    names = {}
    for line in out.stdout.splitlines():
        name, _, value = line.partition("\t")
        name = name.strip()
        key = normalize(value)
        if not name or name == "missing value" or not key:
            continue
        names.setdefault(key, [])
        if name not in names[key]:
            names[key].append(name)
    if not any(len(key) >= 7 or "@" in key for key in names):
        # An address book without a single phone number or email means the read went wrong.
        _failed_at = now
        return "Contacts came back with no phone numbers or emails"
    # A number shared by several contacts (a family landline) keeps every name, so the
    # whitelist still matches whichever one is on it.
    _book = {key: " / ".join(found) for key, found in names.items()}
    _loaded_at = now
    _failed_at = None
    return None


def entry_count():
    """How many phone numbers / emails were read from Contacts."""
    return len(_book or {})


def resolve(handle):
    """The contact name for a handle, the handle itself if it isn't a contact, or None if
    Contacts couldn't be read (so there's no telling who this is)."""
    if not handle:
        return None
    now = time.time()
    if _book is None or now - _loaded_at > BOOK_REFRESH_SECONDS:
        if _failed_at is None or now - _failed_at >= RETRY_SECONDS:
            problem = load()  # on failure an older copy of the book (if any) keeps being used
            if problem:
                print(f"[contacts] {problem}")
    if _book is None:
        return None
    return _book.get(normalize(handle)) or handle
