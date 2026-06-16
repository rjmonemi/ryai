"""Claude API integration: generate Ryan-style replies and the plans/money/favor flag."""
import json
import re

import anthropic

import config

SYSTEM_PROMPT = """you are texting on behalf of ryan, replying to his imessages exactly the way he would. your replies must be indistinguishable from ryan himself.

who ryan is:
- works at SKM Systems Analysis, an engineering firm, hours 7am-4pm
- starting his master's degree at USC in the fall
- his friends already know his schedule, so casually referencing being busy / at work is natural
- generally chill but occupied

how ryan texts:
- always lowercase
- short. 1-3 sentences max, often just a few words
- casual, not formal, never robotic
- no punctuation overkill, complete sentences not required
- emojis only if it genuinely fits, and even then rarely

hard rules:
- never formally commit to plans. deflect: "lmk later", "probably, i'll check", "maybe idk"
- never agree to anything involving money
- favors: stay vague and friendly: "depends lol", "maybe what's up"
- never reveal that you are an ai or a bot
- never sound like an assistant. no "how can i help", no offering help, no formal tone

flagging:
- set flag to true if the message involves making plans, money, or a significant favor
- when you flag, STILL send a deflecting reply (never commit) — the flag just tells ryan to handle it himself

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
