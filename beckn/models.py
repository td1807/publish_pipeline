"""Pydantic mirrors of the Beckn v2 core schemas we actually emit.

Transcribed from beckn.yaml at tag `core-v2.0.0-lts`
(`api/v2.0.0/beckn.yaml`, `components.schemas`). Where that file says
`additionalProperties: false` these models say `extra="forbid"`, so an
accidental extra key fails here rather than at somebody else's validator.

The one deliberate exception is `Attributes`, which the spec defines as an open
JSON-LD container (`additionalProperties: true`, requiring only `@context` and
`@type`). That openness is the entire extension point of Beckn v2, and it is
where every agriculture-specific field in this pipeline lives.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Descriptor(BaseModel):
    """beckn.yaml: Descriptor. additionalProperties: false."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    code: str | None = None
    name: str | None = None
    shortDesc: str | None = None
    longDesc: str | None = None
    thumbnailImage: str | None = None


class TimePeriod(BaseModel):
    """beckn.yaml: TimePeriod. anyOf requires startDate | endDate | (start+end time)."""

    model_config = ConfigDict(extra="forbid")

    startDate: str | None = None
    endDate: str | None = None


class Attributes(BaseModel):
    """beckn.yaml: Attributes — the JSON-LD extension container.

    `@context` and `@type` are REQUIRED by the spec; everything else is
    domain-specific and free-form. Do not add typed fields here: the point of
    this class is that the domain owns the payload.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    context: str = Field(alias="@context")
    type: str = Field(alias="@type")

    @field_validator("context", "type")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("@context and @type must be non-empty (beckn.yaml: Attributes)")
        return v


class Resource(BaseModel):
    """beckn.yaml: Resource. Only `id` is required."""

    model_config = ConfigDict(extra="forbid")

    id: str
    descriptor: Descriptor | None = None
    resourceAttributes: Attributes | None = None


class Provider(BaseModel):
    """beckn.yaml: Provider. Requires id + descriptor. additionalProperties: false.

    `availableAt` is intentionally NOT declared. The spec's Location requires a
    `geo` GeoJSON geometry, and we hold no surveyed boundary for any Indian
    state. Publishing a hand-drawn bounding box would put a fabricated polygon
    on the network that nothing downstream could distinguish from a real
    boundary. Coverage is expressed as governed area codes inside
    `resourceAttributes.coverageAreas` instead. See README "Known limits".
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    descriptor: Descriptor
    providerAttributes: Attributes | None = None


class Catalog(BaseModel):
    """beckn.yaml: Catalog. Requires id + descriptor + provider, anyOf(resources, offers)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    descriptor: Descriptor
    provider: Provider
    isActive: bool = True
    validity: TimePeriod | None = None
    resources: list[Resource] = Field(default_factory=list)

    @field_validator("resources")
    @classmethod
    def _anyof(cls, v: list[Resource]) -> list[Resource]:
        # We never emit `offers`, so `resources` carries the anyOf obligation.
        if not v:
            raise ValueError(
                "Catalog must carry at least one resource (beckn.yaml: Catalog.anyOf)"
            )
        return v


class Context(BaseModel):
    """beckn.yaml: Context.

    Two things to know about `domain`:
      * Beckn v2's Context has NO `domain` property — v2 replaced it with
        `schemaContext`. It is present here because the target
        `message_update.json` carries it.
      * The spec does not set `additionalProperties: false` on Context, so an
        extra key is tolerated rather than invalid.
    It is emitted as a documented house extension, not smuggled in.
    """

    model_config = ConfigDict(extra="allow")

    domain: str
    action: str
    version: Literal["2.0.0"]
    transactionId: str
    messageId: str
    timestamp: str
    schemaContext: list[str] = Field(default_factory=list)


class PublishDirective(BaseModel):
    """beckn.yaml: CatalogPublishAction.publishDirectives[]. Requires catalogId + catalogType."""

    model_config = ConfigDict(extra="forbid")

    catalogId: str
    catalogType: Literal["MASTER", "REGULAR"]
    visibleTo: list[str] = Field(default_factory=list)


class PublishMessage(BaseModel):
    """beckn.yaml: CatalogPublishAction. Requires catalogs (minItems 1)."""

    model_config = ConfigDict(extra="forbid")

    catalogs: list[Catalog]
    publishDirectives: list[PublishDirective] = Field(default_factory=list)

    @field_validator("catalogs")
    @classmethod
    def _at_least_one(cls, v: list[Catalog]) -> list[Catalog]:
        if not v:
            raise ValueError("catalogs must have at least one entry (minItems: 1)")
        return v


class PublishEnvelope(BaseModel):
    """The whole /catalog/publish body — the shape of message_update.json."""

    model_config = ConfigDict(extra="forbid")

    context: Context
    message: PublishMessage

    def to_wire(self) -> dict[str, Any]:
        """Serialize with JSON-LD aliases (`@context`, `@type`) restored."""
        return self.model_dump(by_alias=True, exclude_none=True, mode="json")
