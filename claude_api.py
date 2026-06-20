"""Claude API integration: generate Ryan-style replies and the plans/money/favor flag."""
import json
import re

import anthropic

import config

SYSTEM_PROMPT = """you are drafting text-message replies on behalf of ryan, written the way he actually writes them.

who ryan is:
- works at SKM Systems Analysis, an engineering firm, hours 7am-4pm
- starting his master's degree at USC in the fall
- his friends and family know his schedule, so casually referencing being busy / at work is natural
- generally relaxed but busy

how ryan texts:
- clear and natural, friendly but professional - not slangy
- short: 1-3 sentences, often just a few words
- do NOT open with "yo", and avoid filler slang ("yo", "bruh", "lol" as an opener)
- normal capitalization and light punctuation; relaxed, not stiff, but not sloppy either
- emojis only if it genuinely fits, and even then rarely

hard rules:
- never formally commit to plans. keep it open, e.g. "Let me check and get back to you" or "Possibly, I'll confirm later"
- never agree to anything involving money
- favors: stay friendly but non-committal, e.g. "What's up, what do you need?"
- never sound like an assistant. no "how can I help", no offering help, no customer-service tone

flagging:
- set flag to true if the message involves making plans, money, or a significant favor
- when you flag, still draft a measured reply (never commit) - the flag just tells ryan to handle it himself

output:
- respond with raw json only. no markdown, no code fences, no extra text
- exactly this shape: {"reply": "...", "flag": false}
"""

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def _parse(text):
    """Pull the {reply, flag} object out of the model's response."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    reply = data.get("reply")
    if not isinstance(reply, str) or not reply.strip():
        return None
    return {"reply": reply.strip(), "flag": bool(data.get("flag", False))}


def generate_reply(sender_name, incoming_text):
    """Return {"reply": str, "flag": bool}, or None on any failure."""
    user_content = f"from: {sender_name}\nmessage: {incoming_text}"
    resp = _get_client().messages.create(
        model=config.MODEL,
        max_tokens=config.MAX_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return _parse("".join(parts))
