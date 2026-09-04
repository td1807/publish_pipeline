"""THE single extraction pass. Both branches of step 2 read from its output.

This is the most important design decision in the package, so it is worth being
explicit about why it is one pass and not two.

A `Passage` carries, in one object:
  * `text`   — what branch 2b embeds into the vector DB
  * facets   — what branch 2a publishes as `resourceAttributes`
  * `resource_id` — the join between them

If 2a and 2b each ran their own extraction, they would drift. The failure is
not cosmetic: discovery would route a farmer's question to a resource whose
stored passages carry a *different* filter, so the provider node would search
inside a resource it had just advertised and find nothing. One pass makes that
class of bug unrepresentable, and `tests/test_v4.py::test_facet_parity` holds
the line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..config import MAX_PASSAGE_CHARS, MIN_PASSAGE_CHARS, PROVIDER_ID, DOMAIN
from ..taxonomy.ids import point_id_for, resource_id_for
from ..taxonomy.vocab import Area, Subject, Vocabulary, category_for, load_vocabulary
from . import language
from .pdf_text import Document, UnusableDocument


@dataclass(frozen=True)
class Passage:
    """One indexable unit of a bulletin, with its facets already resolved."""

    text: str
    document: str
    page: int          # 1-based, as cited to a user
    ordinal: int       # position within the document, for a stable id
    language: str
    subjects: tuple[Subject, ...]
    area: Area
    also_covers: tuple[Area, ...]
    category: str
    topics: tuple[str, ...]
    weather_parameters: tuple[str, ...]
    resource_id: str
    point_id: str

    @property
    def citation(self) -> str:
        return f"{self.document} p.{self.page}"

    def facets(self) -> dict:
        """The canonical facet view. 2a publishes it; 2b stores it verbatim.

        Both branches call THIS method, so parity is structural rather than
        maintained by convention.
        """
        return {
            "language": self.language,
            "category": self.category,
            "area_code": self.area.code,
            "area_level": self.area.level,
            "area_name": self.area.name,
            "also_area_codes": [a.code for a in self.also_covers],
            "subject_uris": [s.uri for s in self.subjects],
            "topics": list(self.topics),
            "weather_parameters": list(self.weather_parameters),
            "resource_id": self.resource_id,
        }


@dataclass(frozen=True)
class ExtractionReport:
    """What the rules managed, and — more usefully — what they did not.

    Printed after every run. A state whose district table is thin shows up here
    as a number, rather than silently publishing everything at state level and
    leaving a consumer unable to narrow down.
    """

    document: str
    state_code: str
    state_name: str
    pages: int
    passages: int
    with_subject: int
    with_district: int
    languages: dict[str, int]
    categories: dict[str, int]
    districts: int
    encoding: language.EncodingReading
    warnings: tuple[str, ...]

    @property
    def subject_resolution(self) -> float:
        return self.with_subject / max(self.passages, 1)

    @property
    def district_resolution(self) -> float:
        return self.with_district / max(self.passages, 1)

    def summary(self) -> str:
        langs = ", ".join(f"{k}={v}" for k, v in sorted(self.languages.items()))
        cats = ", ".join(f"{k}={v}" for k, v in sorted(self.categories.items()))
        return (
            f"{self.document}\n"
            f"  state        {self.state_code} ({self.state_name})\n"
            f"  pages        {self.pages}\n"
            f"  passages     {self.passages}\n"
            f"  subjects     {self.subject_resolution:.1%} of passages resolved to the taxonomy\n"
            f"  districts    {self.district_resolution:.1%} of passages placed in a district "
            f"({self.districts} distinct)\n"
            f"  languages    {langs}\n"
            f"  categories   {cats}\n"
            f"  encoding     {self.encoding.summary()}"
        )


class UnknownState(UnusableDocument):
    """The document names no state this vocabulary covers.

    A subclass of UnusableDocument because it is the same kind of answer: the
    document cannot be published, and the caller that already narrates a
    refusal for a scan should narrate this one too rather than stop the run.
    """


# --- which state is this bulletin about? -------------------------------------
# Checked against the document text, not only the filename, so a renamed file
# cannot silently publish under the wrong state code.
_STATE_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("IN-KA", ("karnataka", "ಕರ್ನಾಟಕ")),
    ("IN-UP", ("uttar pradesh", "उत्तर प्रदेश", "उत्तर प्रदेि", "lucknow")),
    ("IN-RJ", ("rajasthan", "राजस्थान")),
)


def detect_state(doc: Document, vocab: Vocabulary) -> str:
    head = "\n".join(p.text for p in doc.pages[:4]).lower()
    name = doc.path.name.lower()
    for code, markers in _STATE_MARKERS:
        if any(m in name for m in markers) or any(m in head for m in markers):
            return code
    raise UnknownState(
        f"Cannot determine which state {doc.path.name} covers. Add a marker to "
        "_STATE_MARKERS in ingest/passages.py rather than guessing — an area "
        "code is a claim about coverage."
    )


# District section headings. Karnataka spells these in English; the Hindi
# bulletins use जिला/जनपद. A passage under such a heading belongs to that
# district even if the district name is never repeated inside the passage.
_DISTRICT_HEADING = re.compile(
    r"(?:agro-?met\s+advisor\w*(?:\s+service)?(?:\s+bulletin)?\s+for\s+(?:the\s+)?"
    r"(?P<en>[A-Za-z /&()\-]{3,60}?)\s+district"
    r"|(?P<hi>[ऀ-ॿ][ऀ-ॿ \-]{2,40})\s*(?:जिला|जनपद|िनपद))",
    re.I,
)


def _heading_districts(line: str, state_code: str, vocab: Vocabulary) -> list[Area]:
    """All districts named by a section heading, in order.

    A heading can name two ("Agromet Advisory for Chitradurga/Davangere
    districts"). The first becomes the passage's primary area; the rest are
    real coverage and are kept in `also_covers` rather than dropped.
    """
    m = _DISTRICT_HEADING.search(line)
    if not m:
        return []
    raw = (m.group("en") or m.group("hi") or "").strip()
    if not raw:
        return []
    out: list[Area] = []
    for part in re.split(r"[/,&]| and ", raw):
        for area in vocab.districts_in(part.strip(), state_code):
            if area not in out:
                out.append(area)
    return out


_BLOCK = re.compile(r"\n\s*\n")


def _blocks(text: str) -> list[str]:
    """Split a page into candidate passages, then merge to a workable size.

    Merging matters for these files: an IMD advisory table emits many short
    lines, and a two-line fragment embeds into a vector that matches almost
    anything. Merging up to MAX_PASSAGE_CHARS keeps a passage long enough to
    carry meaning and short enough to cite a single page honestly.
    """
    out: list[str] = []
    buf = ""
    for raw in _BLOCK.split(text):
        piece = raw.strip()
        if not piece:
            continue
        if not buf:
            buf = piece
        elif len(buf) + len(piece) + 2 <= MAX_PASSAGE_CHARS:
            buf = f"{buf}\n\n{piece}"
        else:
            out.append(buf)
            buf = piece
        while len(buf) > MAX_PASSAGE_CHARS:
            cut = buf.rfind("\n", 0, MAX_PASSAGE_CHARS)
            if cut <= 0:
                cut = MAX_PASSAGE_CHARS
            out.append(buf[:cut].strip())
            buf = buf[cut:].strip()
    if buf:
        out.append(buf)
    return [b for b in out if len(b) >= MIN_PASSAGE_CHARS]


def extract(
    doc: Document,
    *,
    provider_id: str = PROVIDER_ID,
    domain: str = DOMAIN,
    vocab: Vocabulary | None = None,
) -> tuple[list[Passage], ExtractionReport]:
    vocab = vocab or load_vocabulary()
    state_code = detect_state(doc, vocab)
    state = vocab.state(state_code)

    passages: list[Passage] = []
    current_section: list[Area] = []
    ordinal = 0

    for page in doc.pages:
        # Section headings are tracked across pages: an advisory table for
        # Koppal frequently runs onto the next page without repeating its title.
        for line in page.text.split("\n"):
            found = _heading_districts(line, state_code, vocab)
            if found:
                current_section = found

        for block in _blocks(page.text):
            in_block = _heading_districts(block, state_code, vocab)
            if in_block:
                current_section = in_block

            subjects = tuple(vocab.subjects_in(block))
            topics = tuple(vocab.topics_in(block))
            weather = tuple(vocab.weather_in(block))
            mentioned = vocab.districts_in(block, state_code)

            # One PRIMARY area per passage — it decides which resource the
            # passage belongs to — plus every other district the passage
            # genuinely names, kept in `also_covers`:
            #   1. the district section we are inside
            #   2. the single district the passage names
            #   3. the state, when the passage names many districts
            #
            # Case 3 is the norm for the Hindi bulletins, which are organised by
            # agro-climatic zone and list 7-8 districts per row. Publishing such
            # a section at State level is the honest primary claim, but the
            # districts it lists are real coverage, so they travel in
            # `also_covers` and end up in the resource's coverageAreas. Earlier
            # they were simply discarded, which left a consumer unable to narrow
            # by district on a bulletin that names all 75 of them.
            if current_section:
                area, extra = current_section[0], current_section[1:]
            elif len(mentioned) == 1:
                area, extra = mentioned[0], []
            else:
                area, extra = state, list(mentioned)

            category = category_for(list(subjects), list(weather), vocab, list(topics))
            if category == "Weather":
                subjects = ()          # a forecast carries no crop claim
            else:
                weather = ()           # and crop advice carries no forecast claim

            rid = resource_id_for(provider_id, domain, category, area.code)
            passages.append(
                Passage(
                    text=block,
                    document=doc.path.name,
                    page=page.number,
                    ordinal=ordinal,
                    language=language.detect(block).language,
                    subjects=subjects,
                    area=area,
                    also_covers=tuple(a for a in extra if a.code != area.code),
                    category=category,
                    topics=topics,
                    weather_parameters=weather,
                    resource_id=rid,
                    point_id=point_id_for(provider_id, doc.path.name, page.number, ordinal),
                )
            )
            ordinal += 1

    report = _report(doc, state_code, state.name, passages)
    return passages, report


def _report(
    doc: Document, state_code: str, state_name: str, passages: list[Passage]
) -> ExtractionReport:
    langs: dict[str, int] = {}
    cats: dict[str, int] = {}
    for p in passages:
        langs[p.language] = langs.get(p.language, 0) + 1
        cats[p.category] = cats.get(p.category, 0) + 1

    districts = {p.area.code for p in passages if p.area.level == "District"}
    districts |= {
        a.code for p in passages for a in p.also_covers if a.level == "District"
    }
    with_district = sum(
        1
        for p in passages
        if p.area.level == "District"
        or any(a.level == "District" for a in p.also_covers)
    )
    with_subject = sum(1 for p in passages if p.subjects)

    warnings: list[str] = []
    if not districts:
        warnings.append(
            f"No district resolved anywhere in {doc.path.name}: every resource "
            "will publish at State granularity, so a consumer cannot narrow by "
            "district. Extend the district table for this state."
        )
    if passages and with_subject / len(passages) < 0.4:
        warnings.append(
            f"Only {with_subject}/{len(passages)} passages resolved to a "
            "taxonomy subject. Crop coverage claims for this state are thin."
        )

    full = "\n".join(p.text for p in doc.pages)
    enc = language.check_devanagari_encoding(full)
    if not enc.healthy:
        warnings.append(
            f"Devanagari in {doc.path.name} looks mis-encoded ({enc.summary()}). "
            "Exact-term lookup on affected words will fail."
        )

    return ExtractionReport(
        document=doc.path.name,
        state_code=state_code,
        state_name=state_name,
        pages=doc.page_count,
        passages=len(passages),
        with_subject=with_subject,
        with_district=with_district,
        languages=langs,
        categories=cats,
        districts=len(districts),
        encoding=enc,
        warnings=tuple(warnings),
    )
