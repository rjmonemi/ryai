"""Send iMessages via AppleScript (the Messages app)."""
import subprocess

# Handle and text are passed as `on run` arguments (not interpolated into the script),
# so quotes / newlines / etc. in the message can't break or inject AppleScript.
_SCRIPT = """
on run {targetHandle, msgText}
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant targetHandle of targetService
        send msgText to targetBuddy
    end tell
end run
"""


def send(handle, text):
    """Send `text` to `handle` over iMessage. Returns True on success."""
    if not handle or not text:
        return False
    try:
        result = subprocess.run(
            ["osascript", "-e", _SCRIPT, handle, text],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            print(f"[messenger] send failed: {result.stderr.strip()}")
            return False
        return True
    except Exception as e:
        print(f"[messenger] send error: {e}")
        return False
