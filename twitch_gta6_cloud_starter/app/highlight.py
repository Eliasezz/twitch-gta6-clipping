"""Heuristic conversational highlight selector. Does not claim AI emotion detection."""
from __future__ import annotations

import re

REACTION_PHRASES = {
    "wow": 4, "oh my god": 8, "oh my gosh": 7, "no way": 7,
    "unbelievable": 5, "insane": 5, "crazy": 4, "what happened": 4,
    "what": 1, "wait": 3, "look": 2, "bro": 3, "holy": 5,
    "gta": 2, "gta six": 3, "gta 6": 3, "laugh": 2, "shocked": 3,
    "scream": 3, "hilarious": 3, "this is wild": 6,
}


def choose_speech_window(words, source_seconds, clip_seconds, *, stride=2.0):
    """Choose a conversation-rich window, favoring short reaction expressions.

    Words have start/end measured from the start of the source window.
    Return (start_seconds, diagnostics); not a robust virality predictor.
    """
    if source_seconds <= clip_seconds:
        return 0.0, {"method": "entire_video", "reaction_score": 0}
    points = []
    for word in words:
        try:
            start = float(word.get("start", 0))
            token = str(word.get("word", "")).lower()
        except (AttributeError, TypeError, ValueError):
            continue
        token = re.sub(r"[^\w]+", "", token)
        if token:
            points.append((start, token))
    if not points:
        return None, {"method": "no_speech"}

    max_start = max(0.0, float(source_seconds) - float(clip_seconds))
    count = max(1, int(max_start / stride) + 1)
    starts = [min(i * stride, max_start) for i in range(count + 1)]
    best_score, best_start, best_reaction = float("-inf"), 0.0, 0
    for candidate in starts:
        window = [token for timestamp, token in points
                  if candidate <= timestamp < candidate + clip_seconds]
        if not window:
            continue
        text = " " + " ".join(window) + " "
        reactions = sum(weight * len(re.findall(
            r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", text
        )) for phrase, weight in REACTION_PHRASES.items())
        # Speech density remains a weak signal; viral importance is not guaranteed.
        density = min(70, len(window)) * 0.12
        score = 4.0 * reactions + density - candidate * 0.0001
        if score > best_score:
            best_score, best_start, best_reaction = score, candidate, reactions

    return round(best_start, 3), {
        "method": "reaction_keyword_and_speech_density",
        "reaction_score": best_reaction,
    }


def window_words(words, start_seconds, clip_seconds):
    """Convert source timestamps to clip-relative subtitle timestamps."""
    end = start_seconds + clip_seconds
    output = []
    for item in words:
        try:
            first, last = float(item["start"]), float(item["end"])
            word = item["word"]
        except (KeyError, ValueError, TypeError):
            continue
        if last <= start_seconds or first >= end:
            continue
        output.append({
            "word": word,
            "start": round(max(0, first - start_seconds), 3),
            "end": round(min(clip_seconds, last - start_seconds), 3),
        })
    return output
