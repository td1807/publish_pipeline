"""Wrap catalogues in the `/catalog/publish` envelope — the message_update.json shape.

Two documented deviations from `beckn.yaml @ core-v2.0.0-lts`, both inherited
from the target `message_update.json` rather than invented here:

  1. `context.domain` does not exist in Beckn v2's Context schema. v2 replaced
     it with `schemaContext`. Context is not `additionalProperties: false`, so
     an extra key is tolerated — but it is an extension, not spec.
  2. `CatalogPublishAction` is marked `deprecated: true` in core-v2.0.0-lts,
     even though `/catalog/publish` remains the documented publish path and the
     spec offers no replacement for it. We use it and say so.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from ..config import BECKN_VERSION, CATALOG_TYPE, DOMAIN, VISIBLE_TO, schema_context_ref
from .models import Catalog, Context, PublishDirective, PublishEnvelope, PublishMessage

PUBLISH_ACTION = "publish"


def build_envelope(
    catalogs: list[Catalog],
    *,
    domain: str = DOMAIN,
    visible_to: list[str] | None = None,
    catalog_type: str = CATALOG_TYPE,
    transaction_id: str | None = None,
    message_id: str | None = None,
    now: datetime | None = None,
) -> PublishEnvelope:
    if not catalogs:
        raise ValueError("nothing to publish: no catalogues were built")

    now = now or datetime.now(timezone.utc)
    visible_to = list(VISIBLE_TO if visible_to is None else visible_to)

    return PublishEnvelope(
        context=Context(
            domain=domain,
            action=PUBLISH_ACTION,
            version=BECKN_VERSION,
            transactionId=transaction_id or str(uuid.uuid4()),
            messageId=message_id or str(uuid.uuid4()),
            timestamp=now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            # Every distinct capability type present across all catalogues. This
            # is what tells the receiver which JSON-LD vocabularies it needs in
            # order to interpret the resourceAttributes it is about to index.
            schemaContext=_schema_contexts(catalogs),
        ),
        message=PublishMessage(
            catalogs=catalogs,
            publishDirectives=[
                PublishDirective(
                    catalogId=c.id,
                    catalogType=catalog_type,
                    visibleTo=visible_to,
                )
                for c in catalogs
            ],
        ),
    )


def _schema_contexts(catalogs: list[Catalog]) -> list[str]:
    types: set[str] = set()
    for cat in catalogs:
        for res in cat.resources:
            if res.resourceAttributes is None:
                continue
            # "openagrinet:CropAdvisoryCapability" -> "CropAdvisoryCapability"
            types.add(res.resourceAttributes.type.split(":", 1)[-1])
    return [schema_context_ref(t) for t in sorted(types)]
