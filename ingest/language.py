"""Which language is this page in? Unicode ranges, not a model.

The PDFs come from different states, so language is a real variable rather than
a formality. But it is also the place where it is easiest to say something
false, so the mechanics are worth stating plainly:

  * Script is decided by Unicode block membership. Kannada, Gujarati, Tamil,
    Telugu, Malayalam, Odia, Gurmukhi and Bengali each occupy their own block,
    so for those, script IS language and no model is needed.
  * Devanagari is shared by Hindi, Marathi, Nepali, Sanskrit and Konkani. We
    report `hi` and record that the result is ambiguous, rather than pretending
    to have distinguished them. Marathi vs Hindi needs a model; we do not ship
    one, and a wrong confident answer is worse than a flagged one.
  * A page is only non-English if the dominant Indic script clears BOTH an
    absolute character count and a share of all letters. One stray glyph in an
    English table must not flip the page.

Danda (।), double danda (॥) and the rupee sign (₹) are excluded from the count:
they appear inside otherwise-English government text and carry no signal.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..config import LANG_MIN_CHARS, LANG_MIN_RATIO

# (language, script name, Unicode range, sibling languages sharing the script)
_SCRIPTS: tuple[tuple[str, str, tuple[int, int], tuple[str, ...]], ...] = (
    ("hi", "Devanagari", (0x0900, 0x097F), ("hi", "mr", "ne", "sa", "kok")),
    ("bn", "Bengali", (0x0980, 0x09FF), ("bn", "as")),
    ("pa", "Gurmukhi", (0x0A00, 0x0A7F), ()),
    ("gu", "Gujarati", (0x0A80, 0x0AFF), ()),
    ("or", "Odia", (0x0B00, 0x0B7F), ()),
    ("ta", "Tamil", (0x0B80, 0x0BFF), ()),
    ("te", "Telugu", (0x0C00, 0x0C7F), ()),
    ("kn", "Kannada", (0x0C80, 0x0CFF), ()),
    ("ml", "Malayalam", (0x0D00, 0x0D7F), ()),
)

_NEUTRAL = re.compile(r"[।॥₹]")


@dataclass(frozen=True)
class LanguageReading:
    language: str
    script: str
    script_chars: int
    letter_chars: int
    ratio: float
    ambiguous: bool
    siblings: tuple[str, ...]
    reason: str

    def summary(self) -> str:
        amb = f" (ambiguous within {'/'.join(self.siblings)})" if self.ambiguous else ""
        return (
            f"{self.language} · {self.script} · {self.script_chars} script chars "
            f"of {self.letter_chars} letters ({self.ratio:.1%}){amb} — {self.reason}"
        )


def detect(text: str) -> LanguageReading:
    text = _NEUTRAL.sub("", text or "")
    letters = sum(1 for ch in text if ch.isalpha())
    if letters == 0:
        return LanguageReading(
            "en", "Latin", 0, 0, 0.0, False, (), "no letters"
        )

    counts: list[tuple[int, str, str, tuple[str, ...]]] = []
    for lang, script, (lo, hi), siblings in _SCRIPTS:
        n = sum(1 for ch in text if lo <= ord(ch) <= hi)
        if n:
            counts.append((n, lang, script, siblings))

    if not counts:
        return LanguageReading(
            "en", "Latin", 0, letters, 0.0, False, (), "no Indic script present"
        )

    n, lang, script, siblings = max(counts, key=lambda c: c[0])
    ratio = n / letters

    if n < LANG_MIN_CHARS:
        return LanguageReading(
            "en", "Latin", n, letters, ratio, False, (),
            f"{script} present but below {LANG_MIN_CHARS}-char floor",
        )
    if ratio < LANG_MIN_RATIO:
        return LanguageReading(
            "en", "Latin", n, letters, ratio, False, (),
            f"{script} present but below {LANG_MIN_RATIO:.0%} share",
        )

    return LanguageReading(
        lang, script, n, letters, ratio, bool(siblings), siblings,
        f"dominant script is {script}",
    )


def languages_of(texts: list[str]) -> list[str]:
    """Per-item language codes, in order."""
    return [detect(t).language for t in texts]


# --- encoding-quality check --------------------------------------------------
# Separate concern from "which language". These bulletins are set in proper
# Unicode fonts (Nirmala UI, Mangal), but a few glyphs are mis-mapped by the
# producing tool: the UP file emits प्रदेि where प्रदेश belongs. It is a small
# rate, but it is real, and pretending the text is pristine would be wrong.
# Correctly-encoded Hindi is saturated with these function words; mojibake hits
# them only by luck. So the ratio below distinguishes "a few bad glyphs" from
# "this whole page is legacy-font garbage" — two very different problems.
_HINDI_MARKERS = ("के", "में", "और", "से", "है", "को", "का", "की", "पर", "इस")
_HEALTHY_MARKERS_PER_1K = 10.0


@dataclass(frozen=True)
class EncodingReading:
    devanagari_chars: int
    marker_hits: int
    markers_per_1k: float
    healthy: bool

    def summary(self) -> str:
        verdict = "usable Unicode" if self.healthy else "suspect encoding"
        return (
            f"{verdict}: {self.marker_hits} function-word hits over "
            f"{self.devanagari_chars} Devanagari chars "
            f"({self.markers_per_1k:.1f} per 1k)"
        )


def check_devanagari_encoding(text: str) -> EncodingReading:
    dev = sum(1 for ch in text if 0x0900 <= ord(ch) <= 0x097F)
    hits = sum(text.count(m) for m in _HINDI_MARKERS)
    per_1k = 1000 * hits / dev if dev else 0.0
    return EncodingReading(
        devanagari_chars=dev,
        marker_hits=hits,
        markers_per_1k=per_1k,
        healthy=(dev == 0) or (per_1k >= _HEALTHY_MARKERS_PER_1K),
    )

# --- repairing a mis-mapped Devanagari font ----------------------------------
#
# Some producers embed a Devanagari font whose ToUnicode table maps two glyphs
# to each other's code points, and emit the i-matra glyph at its *drawn*
# position -- to the left of the consonant it belongs to -- rather than in
# logical order. Extraction then yields text that looks like Hindi, renders as
# nonsense, and matches nothing: सिरोही arrives as बसरोही, बैंगन as िैंगन.
#
# The repair is a swap and a reorder, which is cheap. Deciding *whether* to
# apply it is the part that has to be careful, because the same transform run
# over healthy text destroys it -- measured, not assumed: on the UP bulletin it
# would lose 43 of 121 recognisable vocabulary terms. So repair_devanagari()
# only transforms; the caller scores both versions against known terms and
# keeps the repaired one only when it strictly wins. A document with a
# different defect, or none, is left exactly as it was.

_GLYPH_SWAP = str.maketrans({"ब": "ि", "ि": "ब"})
_LEADING_I_MATRA = re.compile(r"ि([\u0915-\u0939])")
# The same producer also emits a spurious ii-matra before an anusvara, and a
# spurious virama before a final ii-matra: संभावना arrives as सींभावना, भिंडी as
# भिींड्ी once the swap is undone. Undoing these is not free -- a word that
# legitimately ends in "ीं", such as नहीं, is damaged by the first rule. That is
# the whole reason the caller scores the result instead of trusting it: on a
# document with this defect the crop and district names recovered outnumber the
# words harmed, and on any other document the transform loses and is discarded.
_SPURIOUS = (("ीं", "ं"), ("्ी", "ी"))


def repair_devanagari(text: str) -> str:
    """Undo the swapped-glyph, visual-order i-matra defect. Pure transform."""
    out = _LEADING_I_MATRA.sub(r"\1ि", text.translate(_GLYPH_SWAP))
    for wrong, right in _SPURIOUS:
        out = out.replace(wrong, right)
    return out


@dataclass(frozen=True)
class RepairReading:
    """Whether the repair was applied, and what it was worth."""

    applied: bool
    terms_before: int
    terms_after: int

    @property
    def recovered(self) -> int:
        return self.terms_after - self.terms_before

    def summary(self) -> str:
        if not self.applied:
            return "not needed (no gain in recognisable terms)"
        return (
            f"applied — {self.recovered} more vocabulary term(s) now match "
            f"({self.terms_before} → {self.terms_after})"
        )


def score_terms(text: str, terms: Iterable[str]) -> int:
    """How many known terms occur in this text. The repair's only judge."""
    return sum(1 for t in terms if t in text)
