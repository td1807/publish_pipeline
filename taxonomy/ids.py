"""Identifier rules, in one place because both branches of step 2 depend on them.

A resource id is a pure function of *(provider, domain, category, area)* — and
deliberately not of the document, the bulletin number, or the run time.

That is what makes next Friday's reissue of bulletin 70/2026 **update** the same
resources instead of minting a second set. A farmer does not look for
"bulletin 69/2026"; they look for crop advice for their district. If the id
carried the bulletin number, every publish would create duplicates and every
consumer's cache would fill with near-identical resources.

The vector point id is a pure function of *(provider, document, page, ordinal)*
for the same reason: re-onboarding the same PDF upserts the same points rather
than appending a second copy of every passage.
"""

from __future__ import annotations

import re
import uuid

# Any fixed UUID works as a namespace; it only has to stay constant across runs.
POINT_NAMESPACE = uuid.UUID("6f1d3c62-1f2a-5b7e-9c11-0a2b3c4d5e6f")


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


def resource_id_for(provider_id: str, domain: str, category: str, area_code: str) -> str:
    """Stable across reissues. See module docstring."""
    return "-".join(
        [
            "res",
            slugify(provider_id.removeprefix("prov-")),
            slugify(domain),
            slugify(category),
            slugify(area_code),
        ]
    )


def catalog_id_for(provider_id: str, domain: str, state_code: str) -> str:
    return "-".join(
        ["cat", slugify(provider_id.removeprefix("prov-")), slugify(domain), slugify(state_code)]
    )


def point_id_for(provider_id: str, document: str, page: int, ordinal: int) -> str:
    """Deterministic vector id, so re-ingest replaces instead of duplicating."""
    return str(uuid.uuid5(POINT_NAMESPACE, f"{provider_id}:{document}:{page}:{ordinal}"))
