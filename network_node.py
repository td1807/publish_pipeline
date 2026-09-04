"""A stand-in for the Beckn network layer. What it holds, and what it cannot.

This is a **model, not an implementation**: no registry lookup, no signature
verification, no multi-provider fan-out, and persistence is a dict. It exists to
make one architectural fact concrete and testable.

The network layer receives `/catalog/publish` and stores the
`resourceAttributes` of every resource — all of it, verbatim. Those attributes
are then the *only* thing it can match a consumer's question against. What it
does **not** hold is document text.

That single constraint is where the two-hop shape of the whole system comes
from. The network can answer "who covers red gram in Koppal, in Kannada?"
because that is all metadata. It cannot answer "so what do I spray?" because
the answer is prose that lives in the provider's own vector index. So the
consumer asks the network first, gets an address, and then asks the provider
directly.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field

from .beckn.models import PublishEnvelope


@dataclass(frozen=True)
class Ack:
    """beckn.yaml: Ack / CatalogProcessingResult (status ACCEPTED|REJECTED|PARTIAL)."""

    status: str
    catalog_ids: tuple[str, ...]
    resources_indexed: int
    attributes_bytes: int
    message: str = ""

    def summary(self) -> str:
        return (
            f"{self.status} — {len(self.catalog_ids)} catalogue(s), "
            f"{self.resources_indexed} resource(s) indexed, "
            f"{self.attributes_bytes:,} bytes of resourceAttributes held"
        )


@dataclass
class DiscoveryMatch:
    resource_id: str
    catalog_id: str
    provider_id: str
    score: int
    matched_on: tuple[str, ...]
    languages: tuple[str, ...]
    area_codes: tuple[str, ...]


@dataclass
class NetworkNode:
    """In-process catalogue store: resource_id -> resourceAttributes."""

    attributes: dict[str, dict] = field(default_factory=dict)
    owners: dict[str, tuple[str, str]] = field(default_factory=dict)  # rid -> (catalog, provider)

    # -- step 3: receive a publish -----------------------------------------
    def publish(self, envelope: PublishEnvelope) -> Ack:
        visible: dict[str, list[str]] = {
            d.catalogId: list(d.visibleTo) for d in envelope.message.publishDirectives
        }
        indexed = 0
        for cat in envelope.message.catalogs:
            if cat.id not in visible:
                # The spec requires a directive's catalogId to match a catalog
                # in the same request; a catalogue with no directive is still
                # publishable (directives are optional) so this is not an error.
                visible[cat.id] = []
            for res in cat.resources:
                if res.resourceAttributes is None:
                    continue
                # Stored verbatim and deep-copied: the round-trip check below is
                # only meaningful if we cannot accidentally alias the publisher's
                # own objects.
                self.attributes[res.id] = copy.deepcopy(
                    res.resourceAttributes.model_dump(by_alias=True, exclude_none=True)
                )
                self.owners[res.id] = (cat.id, cat.provider.id)
                indexed += 1

        return Ack(
            status="ACCEPTED",
            catalog_ids=tuple(c.id for c in envelope.message.catalogs),
            resources_indexed=indexed,
            attributes_bytes=len(
                json.dumps(self.attributes, ensure_ascii=False).encode("utf-8")
            ),
        )

    # -- what it can answer -------------------------------------------------
    def discover(
        self,
        *,
        subject_uri: str | None = None,
        area_code: str | None = None,
        language: str | None = None,
        capability_type: str | None = None,
        limit: int = 5,
    ) -> list[DiscoveryMatch]:
        """Match on resourceAttributes only. This is the network's whole ability."""
        out: list[DiscoveryMatch] = []
        for rid, attrs in self.attributes.items():
            score = 0
            matched: list[str] = []

            areas = [a.get("areaCode") for a in attrs.get("coverageAreas", [])]
            langs = attrs.get("languages", [])
            subjects = [s.get("subjectId") for s in attrs.get("agricultureSubjects", [])]

            if subject_uri:
                if subject_uri not in subjects:
                    continue
                score += 2
                matched.append("subject")
            if area_code:
                if area_code not in areas:
                    continue
                score += 2
                matched.append("area")
            if language:
                if language not in langs:
                    continue
                score += 1
                matched.append("language")
            if capability_type:
                if attrs.get("@type", "").split(":")[-1] != capability_type:
                    continue
                score += 1
                matched.append("capability")

            # A more specific claim wins: a district resource outranks a
            # statewide one for the same question.
            if attrs.get("geographicGranularity") == "District":
                score += 1

            cat, prov = self.owners[rid]
            out.append(
                DiscoveryMatch(
                    resource_id=rid,
                    catalog_id=cat,
                    provider_id=prov,
                    score=score,
                    matched_on=tuple(matched),
                    languages=tuple(langs),
                    area_codes=tuple(a for a in areas if a),
                )
            )

        out.sort(key=lambda m: (-m.score, m.resource_id))
        return out[:limit]

    def resource_attributes(self, resource_id: str) -> dict | None:
        """Any one attributes object, verbatim, as stored."""
        return self.attributes.get(resource_id)

    # -- what it demonstrably does NOT hold ---------------------------------
    def contains_text(self, needle: str) -> bool:
        """True if any advisory prose reached the network layer. Must stay False."""
        return needle.lower() in json.dumps(self.attributes, ensure_ascii=False).lower()

    def facet_index(self) -> dict[str, int]:
        """The aggregate the network can actually search over."""
        subjects, areas, topics, cats, params, langs = set(), set(), set(), set(), set(), set()
        for attrs in self.attributes.values():
            subjects.update(s.get("subjectId") for s in attrs.get("agricultureSubjects", []))
            areas.update(a.get("areaCode") for a in attrs.get("coverageAreas", []))
            topics.update(attrs.get("topics", []))
            cats.update(attrs.get("subjectCategories", []))
            params.update(attrs.get("weatherParameters", []))
            langs.update(attrs.get("languages", []))
        return {
            "resources": len(self.attributes),
            "subject_uris": len({s for s in subjects if s}),
            "area_codes": len({a for a in areas if a}),
            "topics": len(topics),
            "subject_categories": len(cats),
            "weather_parameters": len(params),
            "languages": len(langs),
        }

    def verify_round_trip(self, envelope: PublishEnvelope) -> tuple[bool, list[str]]:
        """Every published attributes object is held byte-identically."""
        problems: list[str] = []
        for cat in envelope.message.catalogs:
            for res in cat.resources:
                if res.resourceAttributes is None:
                    continue
                sent = res.resourceAttributes.model_dump(by_alias=True, exclude_none=True)
                held = self.attributes.get(res.id)
                if held is None:
                    problems.append(f"{res.id}: not held by the network node")
                elif json.dumps(held, sort_keys=True, ensure_ascii=False) != json.dumps(
                    sent, sort_keys=True, ensure_ascii=False
                ):
                    problems.append(f"{res.id}: attributes differ after transit")
        return (not problems), problems
