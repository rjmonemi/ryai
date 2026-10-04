"""Claude API integration: write Ryan-style replies and flag what Ryan should handle himself."""
import json
import re
import time

import anthropic

import config

SYSTEM_PROMPT = """you're ryan, answering your own texts from your phone. you're not an assistant and you're not helping anyone, you're just a guy texting back. whatever you write gets sent as a text from ryan, so it should read exactly like him.

about ryan:
- works at SKM Systems Analysis (an engineering firm), usually 7am-4pm on weekdays
- starting his master's at USC in the fall
- people know his schedule, so "at work rn" or "just got off" is normal
- chill, a little dry, busy but never rude

how ryan texts:
- all lowercase, always (including "i")
- no period at the end of a text, barely any punctuation, commas only when needed
- short: usually a few words, at most a sentence or two
- matches their energy: a one-word text gets a short reply, a longer thoughtful text can get a bit more
- normal texting words when they fit: yeah, nah, ok, lol, lmao, ngl, fr, idk, bet, tbh, rn, wyd. not in every text, and never stacked
- "lol" isn't punctuation. don't laugh at things that aren't funny
- doesn't always ask a question back. sometimes he just answers or reacts
- no exclamation-point energy, no hyping things up, no "haha!". emojis rarely, at most one
- never uses em dashes, semicolons, lists, or formal words
- sometimes splits a reply into two texts like people do (e.g. "lmao" then "nah im good"). usually one text, sometimes two, rarely three

using the context:
- you get the recent messages from this chat. read them. stay consistent with what ryan already said, don't repeat yourself, and match how ryan talks with this specific person
- you get the current day and time. use it naturally (at work on a weekday afternoon, late at night, the weekend), never announce it
- with parents and family: still lowercase and short, but warmer and cleaner. no swearing, easy on the slang ("ok", "yeah", "sounds good", "love you too")
- if a text is unclear, react like a person would ("wdym", "huh") instead of guessing
- never make up specifics (where ryan is, who he's with, what he did). keep it vague: "out rn", "at work", "just chillin"

hard rules:
- never commit to plans. keep it open: "maybe, ill lyk", "probably, ill check", "idk yet"
- never agree to anything involving money (sending, lending, paying, venmo)
- favors: vague and friendly: "depends whats up", "maybe, what is it"
- never sound like an assistant: no offering help, no "let me know if you need anything", no summarizing, no formal tone

skip (send nothing, messages = []) when:
- the text doesn't need an answer: "ok", "k", "lol", "👍", "night", "ttyl", or anything that naturally ends the exchange
- it's automated: verification codes, delivery updates, promos, spam, scams
- it's serious or heavy: an emergency, someone hurt or sick, bad news, someone upset with ryan. ryan needs to answer these himself
- they ask whether they're texting a bot or an ai, or seem unsure it's really ryan. don't answer that; ryan will

flag = true (ryan gets a notification and checks it himself):
- plans being made or changed, money, a real favor. still send a non-committal reply for these
- anything you skipped because it was serious, or because they asked if it's really ryan

output json only, in this shape: {"messages": ["text 1", "optional text 2"], "flag": false, "skip": false}
"""

# Structured outputs make the API hand back valid JSON in exactly this shape.
_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "messages": {"type": "array", "items": {"type": "string"}},
        "flag": {"type": "boolean"},
        "skip": {"type": "boolean"},
    },
    "required": ["messages", "flag", "skip"],
    "additionalProperties": False,
}

_client = None
_use_schema = True  # turned off if the API ever refuses structured outputs; _parse copes either way


def _get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(
            api_key=config.ANTHROPIC_API_KEY,
            timeout=config.API_TIMEOUT_SECONDS,
            max_retries=2,
        )
    return _client


def check_api_key():
    """One cheap call at startup, so a bad key fails right away instead of on the first text.

    Returns an error message, or None if the key works (or can't be checked right now).
    """
    try:
        _get_client().models.retrieve(config.MODEL)
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError):
        return 'the API key was rejected. re-run:  export ANTHROPIC_API_KEY="sk-ant-..."  with your real key'
    except anthropic.NotFoundError:
        return f"this API key can't use the model {config.MODEL}"
    except anthropic.APIError as e:
        print(f"[claude] couldn't check the API key right now ({e}); continuing")
    return None


def _hour_minute(lt):
    hour = lt.tm_hour % 12 or 12
    return f"{hour}:{lt.tm_min:02d} {'am' if lt.tm_hour < 12 else 'pm'}"


def _clock(t):
    """'sat 3:42 pm', in the Mac's local time."""
    lt = time.localtime(t)
    return f"{time.strftime('%a', lt).lower()} {_hour_minute(lt)}"


def _right_now(t):
    """'saturday, oct 4, 3:42 pm', in the Mac's local time."""
    lt = time.localtime(t)
    return f"{time.strftime('%A, %b', lt).lower()} {lt.tm_mday}, {_hour_minute(lt)}"


def build_prompt(sender_name, history, new_texts, now, unknown=False):
    """The user turn: what time it is, who this is, the recent chat, and what needs an answer."""
    who = sender_name + (" (not in ryan's contacts)" if unknown else "")
    lines = [f"right now it's {_right_now(now)}.", f"you're texting with: {who}", ""]
    if history:
        lines.append("recent messages in this chat, oldest first:")
        for item in history:
            speaker = "ryan" if item["from_me"] else sender_name
            lines.append(f"[{_clock(item['time'])}] {speaker}: {item['text']}")
        lines.append("")
    lines.append(f"{sender_name}'s newest text(s), not answered yet:")
    lines.extend(new_texts)
    return "\n".join(lines)


def generate_reply(sender_name, history, new_texts, now, unknown=False):
    """Return {"texts": [...], "flag": bool, "skip": bool}, or None if Claude's answer was unusable.

    `history` is a list of {"time", "from_me", "text"} dicts, oldest first; `new_texts` are
    their unanswered texts. API errors are raised to the caller.
    """
    global _use_schema
    request = {
        "model": config.MODEL,
        "max_tokens": config.MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user",
                      "content": build_prompt(sender_name, history, new_texts, now, unknown)}],
    }
    client = _get_client()
    if _use_schema:
        try:
            resp = client.messages.create(
                **request,
                output_config={"format": {"type": "json_schema", "schema": _REPLY_SCHEMA}},
            )
        except (anthropic.BadRequestError, TypeError):
            # If the same request goes through without the schema, the schema was the problem
            # (TypeError: an anthropic package too old to know output_config).
            resp = client.messages.create(**request)
            _use_schema = False
            print("[claude] structured output not accepted; reading plain json from now on")
    else:
        resp = client.messages.create(**request)
    parts = [b.text for b in resp.content if getattr(b, "type", None) == "text"]
    return _parse("".join(parts))


def _is_true(value):
    return value is True or (isinstance(value, str) and value.strip().lower() == "true")


def _parse(text):
    """Pull {messages, flag, skip} out of the model's response and clean up each text."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    texts = data.get("messages", data.get("reply"))  # "reply" = the older one-text shape
    if isinstance(texts, str):
        texts = [texts]
    if not isinstance(texts, list):
        texts = []
    texts = [t for t in (humanize(t) for t in texts if isinstance(t, str)) if t]
    texts = [t for i, t in enumerate(texts) if i == 0 or t != texts[i - 1]]  # no "lol" "lol"
    texts = texts[:config.MAX_REPLY_TEXTS]
    skip = _is_true(data.get("skip")) or not texts
    return {"texts": [] if skip else texts, "flag": _is_true(data.get("flag")), "skip": skip}


def humanize(text):
    """Small cleanups so a reply reads like a phone text: lowercase, no trailing period, no em dashes."""
    t = text.strip()
    t = re.sub(r"^ryan\s*:\s*", "", t, flags=re.IGNORECASE)  # stray label from the transcript format
    if len(t) >= 2 and t[0] in "\"“" and t[-1] in "\"”":
        t = t[1:-1].strip()  # the whole text wrapped in quotes
    t = re.sub(r"\s*[—–]\s*", ", ", t)  # nobody types em dashes on a phone
    if config.FORCE_LOWERCASE:
        t = " ".join(w if w.startswith(("http://", "https://", "www.")) else w.lower() for w in t.split())
    else:
        t = " ".join(t.split())
    if t.endswith(".") and not t.endswith(".."):
        t = t[:-1]  # no period at the end, but leave "..." alone
    return t.strip(" ,")
