"""Passage facets -> `resourceAttributes`. This is the heart of branch 2a.

Beckn core deliberately leaves `Resource.resourceAttributes` as an open JSON-LD
object (`Attributes`: requires only `@context` and `@type`,
`additionalProperties: true`). Everything domain-specific therefore lives here,
and this object is *also* the only thing the network layer can match a
consumer's question against — it holds these attributes and nothing else, and
specifically not document text.

So the rule for what belongs in here is precise: a field belongs if a consumer
could plausibly *discover* on it. "Which crops?" yes. "Which districts?" yes.
"What should I spray on Tuesday?" no — that is an answer, not an index entry,
and it goes stale in every consumer's cache the moment the bulletin is reissued.
"""

from __future__ import annotations

from collections import Counter

from ..config import schema_context_url
from ..ingest.passages import Passage
from ..taxonomy.vocab import Area, Vocabulary


def build(passages: list[Passage], vocab: Vocabulary) -> dict:
    """Aggregate one resource's passages into a single JSON-LD attributes object.

    Every passage in `passages` shares a (primary area, category) pair — that is
    what made them one resource — so the area and `@type` are taken from the
    first, while the vocabularies union across all of them.
    """
    if not passages:
        raise ValueError("a resource needs at least one passage")

    head = passages[0]
    cap = vocab.capability_for(head.category)
    capability_type = cap["type"]

    attrs: dict = {
        "@context": schema_context_url(capability_type),
        "@type": f"openagrinet:{capability_type}",
        "subjectCategories": _subject_categories(cap, passages),
        "languages": _languages(passages),
        "coverageAreas": _coverage_areas(passages),
        "geographicGranularity": head.area.level,
    }

    # Attribute groups are a function of the CAPABILITY TYPE, not of whatever
    # the text happened to mention. A crop resource carries agricultureSubjects
    # and no weatherParameters; a forecast resource the reverse. validate.py
    # enforces this, which is what stops a resource advertising a capability it
    # has no data behind.
    groups = set(cap["attribute_groups"])

    if "agricultureSubjects" in groups:
        subjects = _subjects(passages)
        if subjects:
            attrs["agricultureSubjects"] = subjects

    if "weatherParameters" in groups:
        params = _union(p.weather_parameters for p in passages)
        if params:
            attrs["weatherParameters"] = params

    if "topics" in groups:
        topics = _union(p.topics for p in passages)
        if topics:
            attrs["topics"] = topics

    # Provenance of the claim, not the content of it: which document and how
    # many passages stand behind this resource. A consumer deciding whether to
    # trust a provider can see that a resource rests on 40 passages rather than
    # one stray table cell — without receiving any of the text.
    attrs["evidence"] = {
        "sourceDocuments": sorted({p.document for p in passages}),
        "passageCount": len(passages),
        "pageRange": [min(p.page for p in passages), max(p.page for p in passages)],
    }

    # Refresh cadence and horizon are properties of the bulletin series, not of
    # anything we can read off one issue. Declared as configuration and labelled
    # as such rather than inferred from a date we happened to parse.
    attrs["updateFrequency"] = "P1D"
    if capability_type == "WeatherAdvisoryCapability":
        attrs["forecastHorizon"] = "P7D"

    return attrs


def _subject_categories(cap: dict, passages: list[Passage]) -> list[str]:
    """The capability's declared categories, plus any subject type actually present.

    The capability table alone is not enough. A livestock advisory in these
    bulletins routinely also names the fodder crops involved, so the resource
    genuinely carries Crop-typed subjects — and declaring only
    `["Livestock"]` made the object self-contradictory: a consumer filtering
    `subjectCategories == "Crop"` would skip a resource whose
    `agricultureSubjects` contain crops.

    Taking the union keeps the declaration and the contents in agreement.
    `validate.py` now enforces that agreement so this cannot regress.
    """
    categories = set(cap["subject_categories"])
    for p in passages:
        for subject in p.subjects:
            categories.add("Crop" if subject.kind == "crop" else "Livestock")
    return sorted(categories)


def _languages(passages: list[Passage]) -> list[str]:
    """Languages this resource can actually serve, commonest first.

    Taken from what was detected in the passages, not from the state's default
    language: claiming Hindi for a resource whose passages are all English would
    be a promise the provider node cannot keep at follow-up time.
    """
    counts = Counter(p.language for p in passages)
    return [lang for lang, _ in counts.most_common()]


def _coverage_areas(passages: list[Passage]) -> list[dict]:
    """Governed area codes only — never invented coordinates.

    The primary area first, then every additional district the passages named.
    A statewide zone table that lists 8 districts therefore publishes as a State
    resource that a consumer can still narrow against.
    """
    seen: dict[str, Area] = {passages[0].area.code: passages[0].area}
    for p in passages:
        for area in p.also_covers:
            seen.setdefault(area.code, area)
    primary = passages[0].area.code
    ordered = [seen[primary]] + [
        seen[c] for c in sorted(seen) if c != primary
    ]
    return [a.to_beckn() for a in ordered]


def _subjects(passages: list[Passage]) -> list[dict]:
    by_uri = {s.uri: s for p in passages for s in p.subjects}
    return [by_uri[u].to_beckn() for u in sorted(by_uri)]


def _union(groups) -> list[str]:
    out: set[str] = set()
    for g in groups:
        out.update(g)
    return sorted(out)
