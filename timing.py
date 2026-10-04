"""Timing logic: baseline delays, convo detection, and typing gaps."""
import random

import config


def baseline_delay():
    """Baseline mode: a randomized 3-12 minute delay (with extra jitter), in seconds.

    Flat delays are a dead giveaway, so we randomize across the whole window and
    add a little more jitter on top, clamped to never dip below the minimum.
    """
    base = random.uniform(config.BASELINE_MIN_SECONDS, config.BASELINE_MAX_SECONDS)
    jitter = random.uniform(-15, 15)
    return max(config.BASELINE_MIN_SECONDS, base + jitter)


def is_rapid_fire(timestamps):
    """Convo test: their last RAPID_FIRE_COUNT texts all landed within RAPID_FIRE_WINDOW seconds.

    Uses the texts' own timestamps rather than when we noticed them, so a burst still
    counts even if a poll only catches it a few seconds late.
    """
    n = config.RAPID_FIRE_COUNT
    if len(timestamps) < n:
        return False
    recent = sorted(timestamps)[-n:]
    return recent[-1] - recent[0] <= config.RAPID_FIRE_WINDOW


def typing_delay(text):
    """Roughly how long `text` takes to type on a phone: the gap before each extra text of a split reply."""
    seconds = config.TYPING_BASE_SECONDS + len(text) * config.TYPING_SECONDS_PER_CHAR
    return min(config.TYPING_MAX_SECONDS, seconds * random.uniform(0.8, 1.25))
