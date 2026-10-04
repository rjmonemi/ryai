"""Send texts via AppleScript (the Messages app)."""
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

# Same thing over SMS (green bubbles; needs Text Message Forwarding on the iPhone). Kept as
# a separate script so a problem with it can never break the iMessage path.
_SCRIPTS = {
    "iMessage": _SCRIPT,
    "SMS": _SCRIPT.replace("service type = iMessage", "service type = SMS"),
}


def send(handle, text, service=None):
    """Send `text` to `handle`. Returns True on success.

    Tries the service the chat uses first (iMessage, or SMS for SMS/RCS chats), then the other.
    """
    if not handle or not text:
        return False
    order = ["SMS", "iMessage"] if service in ("SMS", "RCS") else ["iMessage", "SMS"]
    errors = []
    for svc in order:
        try:
            result = subprocess.run(
                ["osascript", "-e", _SCRIPTS[svc], handle, text],
                capture_output=True, text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            # It may have gone through anyway; don't risk sending it twice over the other service.
            print(f"[messenger] send timed out ({svc})")
            return False
        except Exception as e:
            print(f"[messenger] send error: {e}")
            return False
        if result.returncode == 0:
            return True
        errors.append(f"{svc}: {result.stderr.strip()}")
    print(f"[messenger] send failed: {' | '.join(errors)}")
    return False
