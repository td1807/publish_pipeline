"""Group passages into resources, and resources into a catalogue.

The unit of publication is a **capability**, not a document: one resource per
*(primary coverage area x capability category)*. "Crop advisory for Koppal" is a
thing a farmer looks for; "bulletin 69/2026" is not.

Because the unit is *(area x category)*, `@type` follows the category, so a
single agromet bulletin publishes several capability types side by side — crop,
livestock, horticulture and weather — each carrying only the attributes its own
type is for.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from ..config import (
    CATALOG_TYPE,
    DOMAIN,
    PROVIDER_CODE,
    PROVIDER_ID,
    PROVIDER_NAME,
)
from ..ingest.passages import Passage
from ..taxonomy.ids import catalog_id_for, resource_id_for
from ..taxonomy.vocab import Vocabulary, load_vocabulary
from . import resource_attributes
from .models import Catalog, Descriptor, Provider, Resource, TimePeriod

# How long a published catalogue claims to be valid. A year from the run date,
# because these bulletin series are ongoing; a reissue updates in place.
VALIDITY_DAYS = 365


def build_catalog(
    passages: list[Passage],
    *,
    state_code: str,
    state_name: str,
    provider_id: str = PROVIDER_ID,
    domain: str = DOMAIN,
    vocab: Vocabulary | None = None,
    now: datetime | None = None,
) -> Catalog:
    vocab = vocab or load_vocabulary()
    now = now or datetime.now(timezone.utc)

    groups: dict[tuple[str, str], list[Passage]] = defaultdict(list)
    for p in passages:
        groups[(p.area.code, p.category)].append(p)

    resources: list[Resource] = []
    for (area_code, category) in sorted(groups):
        members = groups[(area_code, category)]
        rid = resource_id_for(provider_id, domain, category, area_code)

        # The id the passages were stamped with during extraction must be the id
        # the catalogue publishes. If these ever diverge, discovery hands back a
        # resource id whose passages cannot be found — so it is checked, not
        # assumed.
        assert all(m.resource_id == rid for m in members), (
            f"resource id drift for {area_code}/{category}: extraction said "
            f"{sorted({m.resource_id for m in members})}, catalogue says {rid}"
        )

        head = members[0]
        resources.append(
            Resource(
                id=rid,
                descriptor=Descriptor(
                    code=f"{category.upper()}-{area_code}",
                    name=f"{category} advisory, {head.area.name}",
                    shortDesc=_short_desc(category, head.area.name, head.area.level),
                    longDesc=_long_desc(category, head.area.name, members),
                ),
                resourceAttributes=resource_attributes.build(members, vocab),
            )
        )

    return Catalog(
        id=catalog_id_for(provider_id, domain, state_code),
        descriptor=Descriptor(
            code=f"{PROVIDER_CODE}-{state_code}",
            name=f"{state_name} agromet advisory services",
            shortDesc=f"Agromet advisories for {state_name}",
            longDesc=(
                f"District- and state-level agricultural, livestock and weather "
                f"advisories for {state_name}, derived from the agromet advisory "
                f"bulletin series. Metadata only: this catalogue carries coverage "
                f"and capability declarations, never advisory text."
            ),
        ),
        provider=Provider(
            id=provider_id,
            descriptor=Descriptor(code=PROVIDER_CODE, name=PROVIDER_NAME),
        ),
        isActive=True,
        validity=TimePeriod(
            startDate=_iso(now),
            endDate=_iso(now + timedelta(days=VALIDITY_DAYS)),
        ),
        resources=resources,
    )


def _short_desc(category: str, area_name: str, level: str) -> str:
    kind = {
        "Crop": "Crop advisory",
        "Livestock": "Livestock advisory",
        "Horticulture": "Horticulture advisory",
        "Weather": "Weather forecast and warnings",
    }[category]
    return f"{kind} at {level.lower()} granularity for {area_name}"


def _long_desc(category: str, area_name: str, members: list[Passage]) -> str:
    lo, hi = min(m.page for m in members), max(m.page for m in members)
    pages = f"page {lo}" if lo == hi else f"pages {lo}-{hi}"
    passages = "1 indexed passage" if len(members) == 1 else f"{len(members)} indexed passages"
    return (
        f"{category} advisory capability covering {area_name}, standing on "
        f"{passages} (source {pages}). Advisory text itself is served by the "
        f"provider node on direct request, not through this catalogue."
    )


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
