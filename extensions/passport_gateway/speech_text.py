"""Text prepared for speech. The original reply text is kept for subtitles and memory."""
from __future__ import annotations

import re

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")
_URL = re.compile(r"https?://\S+|www\.\S+", re.I)
_EMPHASIS = re.compile(r"(\*\*|__|~~|\*|_)(\S.*?\S|\S)\1")
_LINE_MARK = re.compile(r"^\s{0,3}(?:#{1,6}\s+|>\s?|[-*+]\s+|\d+[.)]\s+)", re.M)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")
_SENTENCE_END = re.compile(r"(?<=[。！？!?；;])|(?<=[.])(?=\s)")
_DETAIL = re.compile(r"详细|详情|展开|仔细|具体|多说|说说看|讲讲|解释|为什么|怎么回事|step|detail|explain", re.I)

DEFAULT_SENTENCES = 3
DETAIL_SENTENCES = 8
MAX_SPOKEN_CHARS = 280


def normalize(text: str) -> str:
    """Strip Markdown/URLs/emoji so TTS reads words, not syntax."""
    text = _FENCE.sub(" 代码略。", text)
    text = _IMAGE.sub(" ", text)
    text = _LINK.sub(r"\1", text)
    text = _URL.sub("链接", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _LINE_MARK.sub("", text)
    text = _EMPHASIS.sub(r"\2", text)
    text = _EMOJI.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def wants_detail(transcript: str) -> bool:
    return bool(_DETAIL.search(transcript or ""))


def prepare(text: str, transcript: str = "") -> str:
    """Normalize and cap to 1-3 short sentences (more when the user asked for detail)."""
    limit = DETAIL_SENTENCES if wants_detail(transcript) else DEFAULT_SENTENCES
    parts = [p.strip() for p in _SENTENCE_END.split(normalize(text)) if p.strip()]
    spoken = "".join(parts[:limit]) if all(re.search(r"[㐀-鿿]", p) for p in parts[:limit]) \
        else " ".join(parts[:limit])
    if len(spoken) > MAX_SPOKEN_CHARS:
        spoken = spoken[:MAX_SPOKEN_CHARS]
        cut = max(spoken.rfind(c) for c in "。！？!?；;，,")
        if cut > 40:
            spoken = spoken[:cut + 1]
    return spoken.strip()


# Bounded mood vocabulary -> extra delivery hint. Model text never reaches provider config;
# only these fixed strings (selected by a validated enum) are appended to the base instruction.
MOOD_HINTS = {
    "neutral": "",
    "pleased": "这一句语气轻快一点，带一点小骄傲。",
    "gentle": "这一句语气温柔平稳，语速稍慢，像在安慰。",
    "serious": "这一句语气认真平稳，不开玩笑。",
    "puzzled": "这一句语气带一点疑惑，但仍然自然平和。",
}


def instructions_for(base: str, mood: str) -> str:
    return (base + MOOD_HINTS.get(mood, "")).strip()
