"""Resolve iMessage handles (phone / email) to contact display names via the Contacts app."""
import re
import subprocess

# handle string -> resolved name. Contacts AppleScript is slow, so we cache aggressively.
_cache = {}

_SCRIPT = """
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


def _query_key(handle):
    """For phone numbers match on the last 10 digits; for emails match the address."""
    if not handle:
        return None
    if "@" in handle:
        return handle.strip()
    digits = re.sub(r"\D", "", handle)
    return digits[-10:] if len(digits) >= 10 else digits


def resolve(handle):
    """Return a display name for a handle, or the handle itself if no match is found. Cached."""
    if not handle:
        return "unknown"
    if handle in _cache:
        return _cache[handle]
    key = _query_key(handle)
    name = None
    if key:
        try:
            out = subprocess.run(
                ["osascript", "-e", _SCRIPT, key],
                capture_output=True, text=True, timeout=20,
            )
            name = out.stdout.strip() or None
        except Exception:
            name = None
    result = name or handle
    _cache[handle] = result
    return result
