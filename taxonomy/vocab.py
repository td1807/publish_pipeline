"""The vocabulary, and the only thing that turns bulletin prose into governed codes.

This module is deliberately the *only* place that decides "this text mentions
red gram" or "this page is about Koppal district". Both branches of step 2 read
their facts from here, through a single extraction pass, so 2a's published
metadata and 2b's stored filters can never disagree.

Rules-first, by design:
  * deterministic  -- the same PDF resolves identically on every run, so a
                      catalogue never churns and republish is a no-op
  * free           -- no model call in the default path
  * auditable      -- every resolution traces to one alias in one JSON file
  * honest         -- unresolved terms stay unresolved and get counted

An LLM never mints a URI here. See README "Extraction is rules-first".
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from ..config import TAXONOMY_BASE

DATA_DIR = Path(__file__).resolve().parent / "data"

# Scheme names for coverageAreas[].codeScheme. States use the real ISO standard.
# Districts do not: see the note in districts.json and README "Known limits".
ISO_STATE_SCHEME = "ISO-3166-2"
DISTRICT_SCHEME = "OPENAGRI-DISTRICT"


@dataclass(frozen=True)
class Subject:
    """One resolved agriculture subject: a crop or an animal."""

    slug: str
    name: str
    kind: str  # "crop" | "livestock"

    @property
    def uri(self) -> str:
        return f"{TAXONOMY_BASE}/{self.kind}/{self.slug}"

    def to_beckn(self) -> dict:
        """The agricultureSubjects[] entry shape used in resourceAttributes."""
        return {
            "subjectId": self.uri,
            "subjectType": "Crop" if self.kind == "crop" else "Livestock",
            "descriptor": {"code": self.slug.upper().replace("-", "_"), "name": self.name},
        }


@dataclass(frozen=True)
class Area:
    """One resolved coverage area, always a governed code -- never coordinates."""

    code: str
    name: str
    level: str  # "State" | "District"
    scheme: str

    def to_beckn(self) -> dict:
        """The coverageAreas[] entry shape used in resourceAttributes."""
        return {
            "codeScheme": self.scheme,
            "areaCode": self.code,
            "areaLevel": self.level,
            "areaName": self.name,
        }


@dataclass(frozen=True)
class Vocabulary:
    subjects: dict[str, Subject]          # alias -> Subject
    subject_by_slug: dict[str, Subject]
    districts: dict[str, dict[str, Area]] # state code -> alias -> Area
    states: dict[str, Area]
    state_language: dict[str, str]
    topics: dict[str, str]                # alias -> canonical topic
    weather: dict[str, str]               # alias -> canonical parameter
    capabilities: dict[str, dict]
    horticulture_slugs: frozenset[str]

    # -- resolution ---------------------------------------------------------
    def devanagari_terms(self) -> frozenset[str]:
        """Every alias written in Devanagari, across crops and districts.

        Used to judge a proposed encoding repair: a repair is worth applying
        only if more of these actually appear afterwards.
        """
        terms = set(self.subjects)
        for table in self.districts.values():
            terms.update(table)
        return frozenset(
            t for t in terms if any(0x0900 <= ord(c) <= 0x097F for c in t)
        )

    def subjects_in(self, text: str) -> list[Subject]:
        """Every crop/animal mentioned, de-duplicated, in stable slug order."""
        found = {
            s.slug: s
            for alias, s in self.subjects.items()
            if _mentions(text, alias)
        }
        return [found[k] for k in sorted(found)]

    def districts_in(self, text: str, state_code: str) -> list[Area]:
        table = self.districts.get(state_code, {})
        found = {
            a.code: a for alias, a in table.items() if _mentions(text, alias)
        }
        return [found[k] for k in sorted(found)]

    def topics_in(self, text: str) -> list[str]:
        return sorted({t for alias, t in self.topics.items() if _mentions(text, alias)})

    def weather_in(self, text: str) -> list[str]:
        return sorted({w for alias, w in self.weather.items() if _mentions(text, alias)})

    def capability_for(self, category: str) -> dict:
        return self.capabilities[category]

    def state(self, state_code: str) -> Area:
        return self.states[state_code]


# Latin aliases must match on word boundaries: "pea" must not fire inside
# "appear", and "gram" must not fire inside "programme". Indic scripts have no
# \b word boundary in the ASCII sense, so those match as plain substrings --
# acceptable because Devanagari/Kannada alias strings are long enough that
# accidental containment is not a practical risk.
_LATIN = re.compile(r"^[a-z0-9 ()\-']+$")


@lru_cache(maxsize=4096)
def _pattern(alias: str) -> re.Pattern | None:
    if _LATIN.match(alias):
        return re.compile(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", re.I)
    return None


def _mentions(text: str, alias: str) -> bool:
    pat = _pattern(alias)
    if pat is not None:
        return pat.search(text) is not None
    return alias in text


def _load(name: str) -> dict:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def _district_code(state_code: str, name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").upper()
    return f"{state_code}-{slug}"


@lru_cache(maxsize=1)
def load_vocabulary() -> Vocabulary:
    crops = _load("crops.json")
    caps = _load("capabilities.json")
    dists = _load("districts.json")

    subjects: dict[str, Subject] = {}
    by_slug: dict[str, Subject] = {}
    for kind, key in (("crop", "crops"), ("livestock", "livestock")):
        for row in crops[key]:
            subj = Subject(slug=row["slug"], name=row["name"], kind=kind)
            by_slug[subj.slug] = subj
            for alias in [row["name"], *row.get("aliases", [])]:
                subjects[_norm(alias)] = subj

    districts: dict[str, dict[str, Area]] = {}
    states: dict[str, Area] = {}
    state_language: dict[str, str] = {}
    for state_code, block in dists.items():
        if state_code.startswith("_"):
            continue
        states[state_code] = Area(
            code=state_code,
            name=block["state_name"],
            level="State",
            scheme=ISO_STATE_SCHEME,
        )
        state_language[state_code] = block.get("primary_language", "en")
        table: dict[str, Area] = {}
        for row in block["districts"]:
            area = Area(
                code=_district_code(state_code, row["name"]),
                name=row["name"],
                level="District",
                scheme=DISTRICT_SCHEME,
            )
            for alias in [row["name"], *row.get("aliases", [])]:
                table[_norm(alias)] = area
        districts[state_code] = table

    topics: dict[str, str] = {}
    for row in caps["topics"]:
        for alias in [row["name"], *row.get("aliases", [])]:
            topics[_norm(alias)] = row["name"]

    weather: dict[str, str] = {}
    for row in caps["weather_parameters"]:
        for alias in [row["name"], *row.get("aliases", [])]:
            weather[_norm(alias)] = row["name"]

    return Vocabulary(
        subjects=subjects,
        subject_by_slug=by_slug,
        districts=districts,
        states=states,
        state_language=state_language,
        topics=topics,
        weather=weather,
        capabilities=caps["capabilities"],
        horticulture_slugs=frozenset(caps["horticulture_slugs"]),
    )


def _norm(alias: str) -> str:
    """Latin aliases fold to lowercase; Indic text is left exactly as authored."""
    return alias.strip().lower()


def category_for(
    subjects: list[Subject],
    weather: list[str],
    vocab: Vocabulary,
    topics: list[str] | None = None,
) -> str:
    """Decide the capability CATEGORY of a passage.

    Order matters and encodes a real editorial rule: a passage that names an
    animal is livestock advice even when it also mentions fodder crops, and a
    passage that names no subject at all but does describe weather is a forecast
    rather than an empty crop resource.

    `topics` is consulted for livestock because these bulletins routinely give
    animal advice without naming a species — "Animals should be vaccinated
    against FMD, Black quarter" names no animal in the vocabulary. Relying on
    subjects alone filed those passages under Weather (they mention rain), so a
    real livestock advisory was published as a forecast capability. The
    LivestockCare topic catches them.
    """
    topics = topics or []
    if any(s.kind == "livestock" for s in subjects):
        return "Livestock"

    crop_slugs = {s.slug for s in subjects if s.kind == "crop"}
    if crop_slugs:
        if crop_slugs <= vocab.horticulture_slugs:
            return "Horticulture"
        return "Crop"

    if "LivestockCare" in topics:
        return "Livestock"
    return "Weather"
