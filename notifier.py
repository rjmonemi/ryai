"""Native macOS notifications via AppleScript."""
import subprocess

_SCRIPT = """
on run {noteTitle, noteSubtitle, noteBody}
    display notification noteBody with title noteTitle subtitle noteSubtitle
end run
"""


def notify(title, subtitle, body):
    """Fire a macOS notification. Best-effort — never raises."""
    try:
        subprocess.run(
            ["osascript", "-e", _SCRIPT, title, subtitle, body],
            capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        print(f"[notifier] error: {e}")
