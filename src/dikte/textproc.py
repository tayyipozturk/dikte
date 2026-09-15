"""Post-processing of raw transcripts: cleanup, hallucination filter,
user replacements, the spoken "submit" phrase, and prompt building."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from .config import Settings

_MAX_PROMPT_CHARS = 600  # Whisper only uses the last ~224 prompt tokens

# Non-speech annotations Whisper sometimes emits: [BLANK_AUDIO], (music), ♪ …
_ANNOTATION_RE = re.compile(
    r"\[[^\]]{0,40}\]"
    r"|\((?:music|müzik|applause|alkış|laughter|laughs|gülüşmeler|silence|sessizlik|"
    r"inaudible|anlaşılmıyor|noise|gürültü)[^)]{0,20}\)"
    r"|\*[^*]{0,30}\*"
    r"|[♪♫]+",
    re.IGNORECASE,
)

# Whole-transcript hallucinations Whisper produces on silence or noise. Only
# phrases nobody dictates on their own are listed ("Teşekkür ederim" is not).
_HALLUCINATIONS = (
    "Altyazı M.K.",
    "Çeviri ve altyazı M.K.",
    "İzlediğiniz için teşekkür ederim.",
    "İzlediğiniz için teşekkür ederiz.",
    "İzlediğiniz için teşekkürler.",
    "Abone ol.",
    "Abone olmayı unutmayın.",
    "Abone olmayı, beğenmeyi ve yorum yapmayı unutmayın.",
    "Kanalıma abone olmayı ve videoyu beğenmeyi unutmayın.",
    "Bu videoyu beğenmeyi ve kanalımıza abone olmayı unutmayın.",
    "Beğen butonuna tıklamayı unutmayın.",
    "Bir sonraki videoda görüşürüz.",
    "Yeni videolarda görüşünceye kadar hoşçakalın.",
    "Instagram'da hoşçakalın.",
    "www.feyyaz.tv",
    "Thanks for watching!",
    "Thank you for watching.",
    "Thanks for watching, please subscribe.",
    "Please subscribe.",
    "Don't forget to subscribe.",
    "Like and subscribe.",
    "Subtitles by the Amara.org community",
    "Translated by Amara.org Community",
    "Transcript Emily Beynon",
    "Captions by GetTranscribed.com",
    "Captioned by Cotter Captioning Services",
    "I'll see you in the next video.",
    "See you in the next video.",
    "Satsang with Mooji",
    "Find out more at aclu.org",
    "AudioJungle",
    "You",
    "The",
    "So",
    "Oh",
    "Uh",
    "Um",
    "Meow",
    "Applause",
    "Copyright",
)

# Hallucinated endings appended after real speech. Removed from the end only.
_TRAILING_RE = re.compile(
    r"[\s,;:–-]*(?:"
    r"(?:çeviri ve )?altyaz[ıi]\s*:?\s*m\.?\s*k\.?"
    r"|(?:subtitles|translated|captions?|captioned) by [^.]{0,40}\.(?:org|com)[^.]{0,20}"
    r"|izlediğiniz için teşekkür(?:ler| ederim| ederiz)"
    r"|thanks? (?:you )?for watching"
    r")[\s.!]*$",
    re.IGNORECASE,
)


def normalize_for_match(text: str) -> str:
    """Case-insensitive, punctuation-free form (Turkish İ/ı safe enough)."""
    folded = text.casefold().replace("̇", "")  # "İ".casefold() adds U+0307
    folded = re.sub(r"['’`]", "", folded)
    folded = re.sub(r"[^\w\s]", " ", folded)
    return " ".join(folded.split())


_HALLUCINATION_SET = frozenset(normalize_for_match(p) for p in _HALLUCINATIONS)


def clean(raw: str) -> str:
    text = _ANNOTATION_RE.sub(" ", raw)
    text = " ".join(text.split())
    return text.lstrip("-–— ").strip()


def is_hallucination(text: str) -> bool:
    normalized = normalize_for_match(text)
    return not normalized or normalized in _HALLUCINATION_SET


def strip_trailing_hallucination(text: str) -> str:
    stripped = _TRAILING_RE.sub("", text).rstrip()
    return stripped if stripped else text


@functools.lru_cache(maxsize=32)
def _compile_replacements(pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[re.Pattern[str], str], ...]:
    compiled = []
    for spoken, written in pairs:
        left = r"(?<!\w)" if re.match(r"\w", spoken[0]) else ""
        right = r"(?!\w)" if re.match(r"\w", spoken[-1]) else ""
        compiled.append((re.compile(left + re.escape(spoken) + right, re.IGNORECASE), written))
    return tuple(compiled)


def apply_replacements(text: str, pairs: tuple[tuple[str, str], ...]) -> str:
    for pattern, written in _compile_replacements(pairs):
        text = pattern.sub(lambda _m, w=written: w, text)
    return text


def extract_submit(text: str, phrases: tuple[str, ...]) -> tuple[str, bool]:
    """If the text ends with a submit phrase ("gönder"), strip it and report it."""
    for phrase in phrases:
        match = re.search(r"(?<!\w)" + re.escape(phrase) + r"[\s.!?]*$", text, re.IGNORECASE)
        if match:
            return text[: match.start()].rstrip(" ,;:-–"), True
    return text, False


@dataclass(frozen=True)
class Processed:
    text: str
    submit: bool  # press Enter after inserting


def process(raw: str, settings: Settings) -> Processed:
    text = clean(raw)
    if is_hallucination(text):
        return Processed("", False)
    text = strip_trailing_hallucination(text)
    text = apply_replacements(text, settings.replacements)
    if settings.mode == "voice":
        # Never press Enter: anyone audible (a video, a colleague) could run commands.
        # Submit phrases are left in the text, since here they are just words.
        return Processed(text.strip(), False)
    text, spoken_submit = extract_submit(text, settings.submit_phrases)
    text = text.strip()
    return Processed(text, spoken_submit or (settings.auto_enter and bool(text)))


def build_prompt(settings: Settings, language: str) -> str:
    base = settings.prompt_en if language == "en" else settings.prompt_tr
    vocabulary = ", ".join(settings.vocabulary)
    prompt = f"{base} {vocabulary}." if vocabulary else base
    prompt = prompt.strip()
    if len(prompt) > _MAX_PROMPT_CHARS:  # keep the tail, like Whisper does
        prompt = prompt[-_MAX_PROMPT_CHARS:]
        prompt = prompt[prompt.find(" ") + 1 :]
    return prompt
