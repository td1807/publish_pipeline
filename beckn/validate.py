"""Refuse to publish a payload we cannot stand behind.

Three independent checks, in increasing order of how much they are ours rather
than the spec's:

  1. **Spec conformance** — required fields per `beckn.yaml`. Mostly enforced by
     the pydantic models already; re-checked here so a hand-built dict cannot
     bypass them.
  2. **Capability coherence** — a resource must not advertise attribute groups
     its `@type` is not for. Not in the spec (the spec cannot know), but the
     difference between a catalogue and a fiction.
  3. **No prose** — the catalogue carries metadata only. Size is the small
     reason. The real one is staleness: a catalogue is cached by every consumer
     that fetched it, so advisory text inside it goes stale silently and there
     is no mechanism to recall it. Advice has to be fetched live from the
     provider node, which is exactly what branch 2b exists to serve.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..taxonomy.vocab import Vocabulary, load_vocabulary
from .models import Catalog, PublishEnvelope

# Attribute groups that only ever belong to one family of capability.
_GROUPS = ("agricultureSubjects", "weatherParameters", "topics")

# Fields inside resourceAttributes drawn from a CLOSED vocabulary. These are
# checked by membership, which is strictly stronger than scanning them for
# prose: an unexpected string fails whether or not it looks like a sentence.
# ("Spraying" is a canonical topic name; it is also a word that any prose
# detector would flag, which is exactly why membership is the right test.)
_CLOSED_VOCAB_FIELDS = frozenset(
    {
        "topics",
        "weatherParameters",
        "subjectCategories",
        "languages",
        "areaLevel",
        "codeScheme",
        "areaCode",
        "areaName",
        "geographicGranularity",
        "updateFrequency",
        "forecastHorizon",
        "subjectId",
        "subjectType",
        "code",
        "name",
        "passageCount",
        "pageRange",
    }
)
_STRUCTURAL = frozenset({"@context", "@type"})

# ISO-8601 duration, for updateFrequency / forecastHorizon.
_DURATION = re.compile(r"^P(?:\d+[YMWD])*(?:T(?:\d+[HMS])+)?$")
_LANG = re.compile(r"^[a-z]{2}$")

# What "prose" means operationally, applied to the fields that are NOT closed
# vocabulary: an advisory imperative or a dosage. Tuned against the real
# bulletins — "Sawai Madhopur" must not trip it, while
# "Spraying of 1% KNO3 @ 4 ml/litre to overcome moisture stress" must.
_ADVISORY_MARKERS = re.compile(
    r"\b(spray(?:ing)?|apply|irrigat\w+|drench|dosage|per\s+(?:hectare|litre|liter|acre)"
    r"|ml/l|kg/ha|g/l|@\s*\d|should\s+be|advised\s+to|carry\s?out)",
    re.I,
)
_LONG_SENTENCE = re.compile(r"(?:\S+\s+){14,}\S+")


class InvalidPayload(ValueError):
    """Raised instead of publishing."""


@dataclass
class ValidationReport:
    resources: int = 0
    catalogs: int = 0
    checked_attribute_objects: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        verdict = "spec valid" if self.ok else f"INVALID ({len(self.errors)} errors)"
        return (
            f"{verdict}: {self.catalogs} catalogue(s), {self.resources} resource(s), "
            f"{self.checked_attribute_objects} resourceAttributes object(s) checked"
        )


def validate_envelope(
    envelope: PublishEnvelope, *, vocab: Vocabulary | None = None
) -> ValidationReport:
    vocab = vocab or load_vocabulary()
    rep = ValidationReport(catalogs=len(envelope.message.catalogs))

    ctx = envelope.context
    if ctx.version != "2.0.0":
        rep.errors.append(f"context.version must be 2.0.0, got {ctx.version!r}")
    if ctx.action != "publish":
        rep.errors.append(f"context.action must be 'publish', got {ctx.action!r}")
    for f in ("transactionId", "messageId", "timestamp"):
        if not getattr(ctx, f, None):
            rep.errors.append(f"context.{f} is required")

    directive_ids = {d.catalogId for d in envelope.message.publishDirectives}
    for cat in envelope.message.catalogs:
        _validate_catalog(cat, vocab, rep)
        if directive_ids and cat.id not in directive_ids:
            rep.errors.append(
                f"publishDirectives has no entry for catalog {cat.id!r} "
                "(spec: catalogId MUST match a catalog id in this request)"
            )

    return rep


def _validate_catalog(cat: Catalog, vocab: Vocabulary, rep: ValidationReport) -> None:
    if not cat.resources:
        rep.errors.append(f"catalog {cat.id!r} has no resources (Catalog.anyOf)")
    if not cat.provider.id or not cat.provider.descriptor:
        rep.errors.append(f"catalog {cat.id!r} provider needs id + descriptor")

    types_by_group = _allowed_groups(vocab)
    seen_ids: set[str] = set()

    for res in cat.resources:
        rep.resources += 1
        if res.id in seen_ids:
            rep.errors.append(f"duplicate resource id {res.id!r} in catalog {cat.id!r}")
        seen_ids.add(res.id)

        attrs = res.resourceAttributes
        if attrs is None:
            rep.errors.append(f"resource {res.id!r} has no resourceAttributes")
            continue
        rep.checked_attribute_objects += 1

        payload = attrs.model_dump(by_alias=True, exclude_none=True)
        cap_type = attrs.type.split(":", 1)[-1]

        allowed = types_by_group.get(cap_type)
        if allowed is None:
            rep.errors.append(f"resource {res.id!r} has unknown @type {attrs.type!r}")
        else:
            for group in _GROUPS:
                if group in payload and group not in allowed:
                    rep.errors.append(
                        f"resource {res.id!r} is a {cap_type} but carries "
                        f"{group!r}, which that capability does not serve"
                    )

        if not payload.get("coverageAreas"):
            rep.errors.append(f"resource {res.id!r} declares no coverageAreas")
        for area in payload.get("coverageAreas", []):
            if "coordinates" in area:
                rep.errors.append(
                    f"resource {res.id!r} coverageArea carries coordinates; this "
                    "pipeline publishes governed area codes only"
                )
            if not area.get("areaCode") or not area.get("codeScheme"):
                rep.errors.append(
                    f"resource {res.id!r} coverageArea needs codeScheme + areaCode"
                )
        if not payload.get("languages"):
            rep.errors.append(f"resource {res.id!r} declares no languages")

        # subjectCategories must agree with the subjects actually carried.
        # Otherwise a consumer filtering on a category skips a resource that
        # does contain that category's subjects.
        declared = set(payload.get("subjectCategories", []))
        present = {
            s.get("subjectType")
            for s in payload.get("agricultureSubjects", [])
            if s.get("subjectType")
        }
        missing = present - declared
        if missing:
            rep.errors.append(
                f"resource {res.id!r} carries {sorted(missing)} subject type(s) "
                f"but declares subjectCategories={sorted(declared)} — a consumer "
                "filtering on the missing category would skip this resource"
            )

        for offence in _closed_vocab_offences(payload, vocab):
            rep.errors.append(f"resource {res.id!r} {offence}")

        for offence in _prose_in(payload):
            rep.errors.append(f"resource {res.id!r} carries prose in {offence}")

        # The resource descriptor is free text by design (Beckn Descriptor has
        # longDesc), but it must still not carry advice — only description.
        if res.descriptor is not None:
            desc = res.descriptor.model_dump(exclude_none=True)
            for key, value in desc.items():
                if isinstance(value, str) and _ADVISORY_MARKERS.search(value):
                    rep.errors.append(
                        f"resource {res.id!r} descriptor.{key} carries advisory "
                        f"text: {value[:60]!r}"
                    )


def _allowed_groups(vocab: Vocabulary) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for cap in vocab.capabilities.values():
        out[cap["type"]] = set(cap["attribute_groups"])
    return out


def _closed_vocab_offences(payload: dict, vocab: Vocabulary) -> list[str]:
    """Every closed-vocabulary value must be a canonical member.

    This is what actually keeps arbitrary text out of the structured fields.
    A topic must BE a topic from capabilities.json; a subjectId must BE a URI
    the taxonomy minted. Nothing gets to be almost-right.
    """
    out: list[str] = []
    topics = set(vocab.topics.values())
    weather = set(vocab.weather.values())
    categories = {c for cap in vocab.capabilities.values() for c in cap["subject_categories"]}
    subject_uris = {s.uri for s in vocab.subject_by_slug.values()}
    schemes = {"ISO-3166-2", "OPENAGRI-DISTRICT"}
    levels = {"State", "District"}

    for t in payload.get("topics", []):
        if t not in topics:
            out.append(f"topic {t!r} is not in the topic vocabulary")
    for w in payload.get("weatherParameters", []):
        if w not in weather:
            out.append(f"weatherParameter {w!r} is not in the vocabulary")
    for c in payload.get("subjectCategories", []):
        if c not in categories:
            out.append(f"subjectCategory {c!r} is not a known category")
    for lang in payload.get("languages", []):
        if not _LANG.match(lang):
            out.append(f"language {lang!r} is not an ISO-639-1 two-letter code")
    if payload.get("geographicGranularity") not in levels:
        out.append(f"geographicGranularity {payload.get('geographicGranularity')!r} invalid")
    for field_name in ("updateFrequency", "forecastHorizon"):
        val = payload.get(field_name)
        if val is not None and not _DURATION.match(val):
            out.append(f"{field_name} {val!r} is not an ISO-8601 duration")
    for area in payload.get("coverageAreas", []):
        if area.get("codeScheme") not in schemes:
            out.append(f"coverageArea codeScheme {area.get('codeScheme')!r} unknown")
        if area.get("areaLevel") not in levels:
            out.append(f"coverageArea areaLevel {area.get('areaLevel')!r} unknown")
    for subj in payload.get("agricultureSubjects", []):
        if subj.get("subjectId") not in subject_uris:
            out.append(
                f"agricultureSubject {subj.get('subjectId')!r} was not minted by "
                "the taxonomy (no code path may invent a subject URI)"
            )
    return out


def _prose_in(payload: dict, path: str = "resourceAttributes") -> list[str]:
    """Flag advisory-looking strings in the fields that are NOT closed vocabulary.

    Closed-vocabulary fields are skipped here because membership already proved
    them exact; this catches anything that arrived in an unexpected key.
    """
    offences: list[str] = []

    def walk(node, where: str, key: str | None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _STRUCTURAL:
                    continue
                walk(v, f"{where}.{k}", k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{where}[{i}]", key)
        elif isinstance(node, str):
            if key in _CLOSED_VOCAB_FIELDS:
                return
            if _ADVISORY_MARKERS.search(node) or _LONG_SENTENCE.search(node):
                offences.append(f"{where} = {node[:60]!r}")

    walk(payload, path, None)
    return offences


def assert_valid(envelope: PublishEnvelope, *, vocab: Vocabulary | None = None) -> ValidationReport:
    rep = validate_envelope(envelope, vocab=vocab)
    if not rep.ok:
        raise InvalidPayload(
            "refusing to publish:\n  " + "\n  ".join(rep.errors[:20])
        )
    return rep
