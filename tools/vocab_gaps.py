"""Which terms in a bulletin did the vocabulary fail to recognise?

    .venv/bin/python tools/vocab_gaps.py --all
    .venv/bin/python tools/vocab_gaps.py data/imd_up_agromet.pdf
    .venv/bin/python tools/vocab_gaps.py --all --json gaps.json

The extraction report already says *how much* a bulletin resolved ("24.1% of
passages resolved to the taxonomy"). It does not say *what* it missed, so the
only way to act on a low number today is to read the whole PDF again by hand.
This turns that percentage into a triage list.

Deliberately independent of `extract()`. Extraction calls `detect_state()`
first, which refuses a bulletin from a state the vocabulary does not cover --
and that refusal is precisely when you most need to know what is missing. This
tool works off `read_document()` alone, so it runs on documents the pipeline
will not publish.

It proposes, it does not decide. Nothing here writes to taxonomy/data/, and
nothing here mints a subject URI. A candidate is a suggestion for a human to
accept or reject, because a wrong crop alias publishes a false coverage claim
and `crops.json` warns about exactly that at the top of the file.

Measured recall, rather than assumed: five crops the Karnataka bulletin plainly
discusses (Sugarcane, Sunflower, Mango, Tomato, Onion) were removed from
crops.json and the scan re-run. All five came back, at ranks 3, 4, 9, 11 and 30
out of 272 candidates -- so all five inside the default --limit 30 view. An
earlier version that looked for a crop adjacent to a growth stage on the same
line found only one of the five, because MuPDF emits each table cell as its own
line and the crop column is therefore a run of bare one-word lines.

Precision is deliberately the looser half of the trade. Roughly half the top 30
is noise a reader dismisses in a second, while a missed crop silently costs
real coverage in the published catalogue. Read it as a triage list, not an
answer.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

if __package__ in (None, ""):  # run as a script from inside the checkout
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from publish_pipeline.config import DATA_DIR
from publish_pipeline.ingest.document_text import UnusableDocument, read_document
from publish_pipeline.taxonomy.vocab import Vocabulary, load_vocabulary

# --- signal 1: the crop column of an IMD advisory table ----------------------
# These bulletins are built around `Major crops | Stage | Pest/disease |
# Advisories` tables, so a term sitting immediately before a growth stage is
# the single most reliable indicator that it is a crop. This is what catches
# "Litchi Fruiting" or "Jute Vegetative" in a bulletin from a new state.
_STAGES = (
    "flowering", "vegetative", "germination", "tillering", "maturity",
    "fruiting", "ripening", "sowing", "transplanting", "nursery",
    "panicle initiation", "grain filling", "boll formation", "milking",
    "dough", "developmental stage", "pre-flowering", "post harvest",
    "post-harvest", "harvesting", "seedling", "budding", "tuber formation",
    "pod formation", "knee high", "reproductive",
)
_STAGE_RE = re.compile(
    r"(?P<term>(?:[A-Z][a-z]+[ /-]{0,2}){1,3})\s*[\(\-–—:]?\s*"
    r"(?:" + "|".join(re.escape(s) for s in _STAGES) + r")\b",
    re.I,
)

# The signal that actually carries recall. MuPDF emits each table cell as its
# own line, so a bulletin's crop column arrives as a run of bare lines --
# "Sunflower", "Tomato", "Onion" -- with the stage on a *separate* line. An
# adjacency rule therefore finds almost nothing (measured: it caught 1 of 6
# crops deliberately hidden from the vocabulary). A short standalone line with
# no verb and no terminal punctuation is a table cell, and that is where the
# crop column lives.
_CELL_RE = re.compile(r"^(?:[A-Z][a-z]+|[a-z]{4,})(?:[ /-][A-Za-z]+){0,2}$")

# "For control of woolly aphid in sugarcane spray 1.0 gm" -- in an advisory
# sentence the crop is the object of a preposition, not the subject of the
# clause, and it is often lower-cased.
_OBJECT_RE = re.compile(r"\b(?:in|of|on|for)\s+([A-Za-z]{4,}(?:[ -][a-z]{3,})?)\b")

# --- signal 2: an advisory line that names no subject we know ----------------
# A line telling a farmer to spray something is about *some* crop. If no
# subject resolves, either the crop is missing from the vocabulary or the line
# is genuinely generic. Ranking by frequency separates the two.
_ADVISORY = re.compile(
    r"\b(spray|drench|apply|irrigat\w+|sow|transplant|prune|harvest\w*|"
    r"छिड़काव|सिंचाई|बुवाई|कटाई|ಸಿಂಪರಣೆ|ನೀರಾವರಿ)\b", re.I
)

# Chemicals are worth excluding rather than reporting: they are real terms the
# vocabulary does not want, and they would otherwise dominate the ranking.
_CHEMICAL = re.compile(
    r"(zim|phos|thrin|mycin|conazole|prid|xam|sulf|sulph|copper|mancozeb|"
    r"neem|urea|potash|borax|gypsum|zinc|sulphur|carb|azole|amide|ate)$", re.I
)

# Verbs, weather vocabulary and boilerplate that lead a line but are not crops.
_STOP = frozenset("""
the and for of in to at on with by from as is are be advised farmers should may
crop crops stage advisory advisories district districts weather rainfall rain
temperature management spray apply water soil plant plants field fields per day
days week light heavy maximum minimum humidity wind speed sky condition forecast
expected during after before agromet bulletin department india meteorological
university krishi vigyan kendra general vegetative flowering sowing harvesting
maturity fruiting ripening control use time state pest disease avoid maintain
provide monitor remove ensure conserve keep give take make store cover spraying
early late morning evening night today tomorrow next last this that there here
very likely places moderate isolated warning intensity distribution probability
scattered thunderstorm regional lightning govt ministry earth sciences centre
normal link plain below above eastern western southern northern humid annexure
many most few actual likely dry wet fair clear cloudy overcast rise fall change
arid irrigated north south east west partial total note table page figure source
farmer advisory issued valid period week ending dated contact email phone website
""".split())

_TITLE = re.compile(r"^[A-Za-z][a-z]+(?:[ /-][A-Za-z]+){0,2}$")

# Plant parts, farm processes, and pest/disease names. These are matched
# against the WHOLE term only, never against a word inside it, so real
# multi-word subjects that happen to contain one -- "Curry leaf", "Fruit fly"
# as distinct from bare "Fruit" -- still survive. Kept generic on purpose:
# tuning this list against the three bundled bulletins would make the tool
# worse on the new-state bulletin it exists to triage.
_NOT_A_SUBJECT = frozenset("""
development preparation growing fruit leaf root stem seed soil fertilizer
fertiliser micronutrient micronutrients moisture nutrient nutrients irrigation
thinning spacing weeding mulching storage marketing advisory advisories agro
general condition damage symptom symptoms infestation incidence management
protection quality yield production area block taluk village orchard nursery
aphids aphid thrips whitefly borer caterpillar larvae mite mites jassid hopper
weevil weevils worm worms blight rust wilt mildew mold mould spot mosaic virus
bacteria fungus fungal insect insects trap traps dose spraying drenching
livestock animals birds plants crops seeds fruits leaves roots
""".split())

# An IMD advisory line opens with an imperative ("Provide protective
# irrigation"), never with the crop, so a leading verb is the single strongest
# sign that a candidate is a sentence rather than a subject. Rejecting on the
# FIRST word specifically -- not on any word -- keeps real two-word crop names
# such as "Bottle gourd" and "Curry leaf".
_LEADING_VERB = frozenset("""
provide apply maintain postpone harvest go do keep give take make store cover
avoid monitor remove ensure conserve destroy spray drench irrigate sow prune
plant grow use continue start stop repeat follow adopt undertake carry complete
after before during if when where while one two three all any some this that
land crop farmers it they there here at in on for with by from as is are be
install place put set fix mix add dilute drain check inspect scout collect
active delayed raised early late kharif rabi zaid summer winter season sowing
plantation fall total partial normal above below light heavy moderate
""".split())

# Devanagari function words, measures and verb endings. Without these the Indic
# signal returns "करें।" (do) and "लीटर" (litre) rather than crop names, because
# those are what an advisory sentence is mostly made of.
_INDIC_STOP = frozenset("""
करें कर करने किया जाए जाये जाता होने होगा रहे रहें लीटर किलो ग्राम मिली प्रति
हेक्टेयर एकड़ दर से का की के को में पर और या भी नहीं तथा एवं लिए साथ बाद पहले
दौरान समय दिन सप्ताह वर्षा वर्ाा बारिश तापमान मौसम फसल फसलों किसान किसानों
सलाह सलाहः दी जानी चाहिए संभावना अनुसार अनुसारः मात्रा छिड़काव कीटनाशकों दवा
उपयोग प्रयोग रोकथाम नियंत्रण अवस्था स्थिति क्षेत्र जिला जिले राज्य विभाग केंद्र
बीज किलोग्राम धकलोग्राम कुंतल क्विंटल मिलीलीटर सेंटीमीटर औसत अधिकतम न्यूनतम
""".split())

# A term that is itself part of the table's stage column, not its crop column.
_STAGE_WORDS = frozenset(w for s in _STAGES for w in s.replace("-", " ").split())

# Function words that never appear mid-name in a real crop or animal.
_FUNCTION = frozenset("""
or the for is at and before after based with of to a an its their this that
""".split())


# Two of the three bulletins are largely Devanagari, and one of them arrives
# with a mis-mapped font. Scanning it unrepaired would report corrupted spellings
# as missing vocabulary, which is worse than useless. This is the same
# score-both-and-keep-the-winner gate `scenario1.repair_encoding()` applies,
# reproduced here rather than imported so the tool does not pull in the beckn
# models (and therefore pydantic) just to read a PDF.
_INDIC_WORD = re.compile(r"[\u0900-\u097F\u0C80-\u0CFF]{3,}")


def _repair_if_it_helps(pages, vocab: Vocabulary):
    from publish_pipeline.ingest.language import repair_devanagari, score_terms

    terms = vocab.devanagari_terms()
    joined = "\n".join(p.text for p in pages)
    before = score_terms(joined, terms)
    repaired = [repair_devanagari(p.text) for p in pages]
    after = score_terms("\n".join(repaired), terms)
    if after <= before:
        return [p.text for p in pages], False
    return repaired, True


def _known_indic(vocab: Vocabulary) -> frozenset[str]:
    """Every Indic alias the vocabulary already knows, in any table."""
    known = {a for a in vocab.subjects if _INDIC_WORD.fullmatch(a.replace(" ", ""))}
    known |= {a for t in vocab.districts.values() for a in t}
    known |= set(vocab.topics) | set(vocab.weather)
    return frozenset(known)


@dataclass
class Candidate:
    term: str
    hits: int = 0
    signals: set[str] = field(default_factory=set)
    pages: set[int] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)

    @property
    def score(self) -> int:
        """Frequency, weighted up when more than one signal agrees."""
        return self.hits * (2 if len(self.signals) > 1 else 1)


def _clean(term: str) -> str:
    """Fold case so "sugarcane" and "Sugarcane" are one candidate, not two."""
    term = re.sub(r"\s+", " ", term).strip(" -/:()").strip()
    return term[:1].upper() + term[1:].lower() if term else term


def _is_plausible(term: str, vocab: Vocabulary, state_hint: str | None) -> bool:
    """Reject what is definitely not a missing subject."""
    if len(term) < 4 or len(term) > 32:
        return False
    words = term.lower().split()
    if term.lower() in _STOP:
        return False
    # A term whose first or last word is already known noise is a fragment of a
    # sentence or a forecast phrase ("Very likely", "Isolated places"), not a
    # subject. Checking both ends is what keeps the multi-word forecast
    # vocabulary of a weather-heavy bulletin out of the ranking.
    if words[0] in _LEADING_VERB or words[0] in _STOP:
        return False
    if len(words) > 1 and words[-1] in _STOP:
        return False
    if any(w in _FUNCTION for w in words):
        return False
    if any(w in _STAGE_WORDS for w in words):   # the stage column, not the crop column
        return False
    if term.lower() in _NOT_A_SUBJECT:   # whole term only -- see the note on that set
        return False
    if _CHEMICAL.search(term):
        return False
    if vocab.subjects_in(term):          # already known
        return False
    if term.lower() in {a.name.lower() for a in vocab.states.values()}:
        return False                          # the state's own name
    for state in ([state_hint] if state_hint else list(vocab.districts)):
        if vocab.districts_in(term, state):   # it is a place, not a crop
            return False
    if not _TITLE.match(term):
        return False
    return True


def scan(path: Path, vocab: Vocabulary, state_hint: str | None = None) -> dict:
    doc = read_document(path)
    page_texts, repaired = _repair_if_it_helps(doc.pages, vocab)
    known_indic = _known_indic(vocab)
    cands: dict[str, Candidate] = {}

    def add(term: str, signal: str, page: int, line: str) -> None:
        term = _clean(term)
        if not _is_plausible(term, vocab, state_hint):
            return
        c = cands.setdefault(term, Candidate(term=term))
        c.hits += 1
        c.signals.add(signal)
        c.pages.add(page)
        if len(c.evidence) < 2:
            c.evidence.append(f"p.{page}  {line.strip()[:110]}")

    advisory_lines = 0
    unresolved_advisory = 0

    indic: Counter = Counter()
    indic_evidence: dict[str, tuple[int, str]] = {}

    for page, text in zip(doc.pages, page_texts):
        for line in text.split("\n"):
            if not line.strip():
                continue

            for m in _STAGE_RE.finditer(line):
                add(m.group("term"), "stage-column", page.number, line)

            stripped = line.strip()
            if 4 <= len(stripped) <= 30 and _CELL_RE.match(stripped):
                add(stripped, "table-cell", page.number, line)

            if _ADVISORY.search(line):
                for m in _OBJECT_RE.finditer(line):
                    add(m.group(1), "advisory-object", page.number, line)

                advisory_lines += 1
                if not vocab.subjects_in(line):
                    unresolved_advisory += 1
                    head = re.match(r"^([A-Z][a-z]+(?:[ /-][A-Za-z]+){0,2})", line.strip())
                    if head:
                        add(head.group(1), "advisory-no-subject", page.number, line)
                    # Indic bulletins carry the subject inside the line rather
                    # than at its head, so collect words instead of a prefix.
                    for raw in _INDIC_WORD.findall(line):
                        w = raw.strip("।॥,.()-")
                        if len(w) < 3 or w in _INDIC_STOP:
                            continue
                        if w in known_indic or any(k in w for k in known_indic):
                            continue
                        # A word carrying a verb ending is a predicate, not a subject.
                        if w.endswith(("ेंगे", "करें", "कर", "ाएं", "ायें", "ना", "ने")):
                            continue
                        indic[w] += 1
                        indic_evidence.setdefault(w, (page.number, line.strip()[:110]))

    for term, n in indic.items():
        if n < 2:            # a single occurrence in an Indic script is usually noise
            continue
        c = cands.setdefault(term, Candidate(term=term))
        c.hits += n
        c.signals.add("indic-advisory")
        pg, line = indic_evidence[term]
        c.pages.add(pg)
        if not c.evidence:
            c.evidence.append(f"p.{pg}  {line}")

    ranked = sorted(cands.values(), key=lambda c: (-c.score, c.term))
    return {
        "document": doc.path.name,
        "pages": doc.page_count,
        "encodingRepairApplied": repaired,
        "advisoryLines": advisory_lines,
        "advisoryLinesWithNoKnownSubject": unresolved_advisory,
        "candidates": ranked,
    }


def _print(report: dict, limit: int) -> None:
    print(f"\n{'=' * 78}\n{report['document']}  ({report['pages']} pages)\n{'=' * 78}")
    adv, un = report["advisoryLines"], report["advisoryLinesWithNoKnownSubject"]
    if report.get("encodingRepairApplied"):
        print("encoding repair applied before scanning (mis-mapped Devanagari font)")
    pct = (100 * un / adv) if adv else 0.0
    print(
        f"advisory lines        {adv}\n"
        f"  naming no known subject  {un} ({pct:.0f}%)  <- the pool these come from\n"
    )
    ranked = report["candidates"][:limit]
    if not ranked:
        print("no unrecognised candidates — the vocabulary covers this bulletin\n")
        return
    print(f"{'candidate':<24} {'hits':>5}  {'pages':>5}  signals")
    print("-" * 78)
    for c in ranked:
        print(
            f"{c.term:<24} {c.hits:>5}  {len(c.pages):>5}  "
            f"{'+'.join(sorted(c.signals))}"
        )
    print("\nevidence for the top few:")
    for c in ranked[:5]:
        print(f"\n  {c.term}")
        for ev in c.evidence:
            print(f"     {ev}")
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="List terms a bulletin uses that the vocabulary does not know."
    )
    ap.add_argument("files", nargs="*", help="paths, or names inside data/")
    ap.add_argument("--all", action="store_true", help="every PDF in data/")
    ap.add_argument("--limit", type=int, default=30, help="candidates shown per document")
    ap.add_argument("--json", metavar="PATH", help="also write the full report as JSON")
    args = ap.parse_args(argv)

    names = args.files or []
    if args.all or not names:
        names = sorted(str(p) for p in DATA_DIR.glob("*.pdf"))
    paths = [p if (p := Path(n)).exists() else DATA_DIR / n for n in names]

    vocab = load_vocabulary()
    reports = []
    for path in paths:
        try:
            rep = scan(Path(path), vocab)
        except UnusableDocument as exc:
            print(f"\nSKIPPED  {Path(path).name}\n         {exc}")
            continue
        except FileNotFoundError:
            print(f"\nSKIPPED  {Path(path).name}\n         no such file")
            continue
        reports.append(rep)
        _print(rep, args.limit)

    if len(reports) > 1:
        combined: Counter = Counter()
        where: dict[str, set[str]] = defaultdict(set)
        for rep in reports:
            for c in rep["candidates"]:
                combined[c.term] += c.hits
                where[c.term].add(rep["document"].removeprefix("imd_")[:12])
        print(f"\n{'=' * 78}\nacross all documents — add these first\n{'=' * 78}")
        for term, n in combined.most_common(15):
            print(f"  {term:<24} {n:>4} hits   seen in: {', '.join(sorted(where[term]))}")
        print()

    if args.json:
        out = [
            {
                **{k: v for k, v in r.items() if k != "candidates"},
                "candidates": [
                    {
                        "term": c.term,
                        "hits": c.hits,
                        "signals": sorted(c.signals),
                        "pages": sorted(c.pages),
                        "evidence": c.evidence,
                    }
                    for c in r["candidates"]
                ],
            }
            for r in reports
        ]
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
        print(f"wrote {args.json}")

    print(
        "These are SUGGESTIONS. Nothing was written to taxonomy/data/. Add a term\n"
        "only after checking the evidence lines: a wrong alias publishes a false\n"
        "coverage claim, and an alias must not be a whole word inside another\n"
        "crop's name (see the header of crops.json).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
