"""Timing logic for the X (rapid) and Y (baseline) reply modes."""
import random

import config


def baseline_delay():
    """Variable Y: a randomized 3-12 minute delay (with extra jitter), in seconds.

    Flat delays are a dead giveaway, so we randomize across the whole window and
    add a little more jitter on top, clamped to never dip below the minimum.
    """
    base = random.uniform(config.BASELINE_MIN_SECONDS, config.BASELINE_MAX_SECONDS)
    jitter = random.uniform(-15, 15)
    return max(config.BASELINE_MIN_SECONDS, base + jitter)


def is_rapid_fire(timestamps, now):
    """Variable X test: >= RAPID_FIRE_COUNT incoming msgs within the last RAPID_FIRE_WINDOW seconds."""
    recent = [t for t in timestamps if now - t <= config.RAPID_FIRE_WINDOW]
    return len(recent) >= config.RAPID_FIRE_COUNT
