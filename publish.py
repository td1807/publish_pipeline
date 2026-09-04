"""Step 3: `/publish` — push the catalogue metadata out to the network layer.

Nothing is computed here. By the time publish is called, branch 2a has already
built and validated the payload; this module only decides *where* it goes and
refuses to send anything that has not been validated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .beckn.models import PublishEnvelope
from .beckn.validate import ValidationReport, assert_valid
from .config import NETWORK_NODE_URL
from .network_node import Ack, NetworkNode

PUBLISH_PATH = "/catalog/publish"


@dataclass(frozen=True)
class PublishResult:
    ack: Ack
    validation: ValidationReport
    payload_bytes: int
    target: str
    round_trip_ok: bool
    round_trip_problems: tuple[str, ...]

    def summary(self) -> str:
        rt = "verified" if self.round_trip_ok else f"FAILED ({len(self.round_trip_problems)})"
        return (
            f"target        {self.target}\n"
            f"validation    {self.validation.summary()}\n"
            f"payload       {self.payload_bytes:,} bytes\n"
            f"ack           {self.ack.summary()}\n"
            f"round-trip    {rt} — every resourceAttributes field held as published"
        )


def publish(
    envelope: PublishEnvelope,
    *,
    node: NetworkNode | None = None,
    url: str = NETWORK_NODE_URL,
) -> tuple[PublishResult, NetworkNode]:
    """Validate, then deliver. Refuses to publish an invalid payload."""
    validation = assert_valid(envelope)

    wire = envelope.to_wire()
    body = json.dumps(wire, ensure_ascii=False)
    payload_bytes = len(body.encode("utf-8"))

    if url:
        ack = _post(url, wire)
        # A real network node is opaque: we cannot inspect what it kept, so we
        # do not claim to have verified a round trip against it.
        return (
            PublishResult(
                ack=ack,
                validation=validation,
                payload_bytes=payload_bytes,
                target=f"{url.rstrip('/')}{PUBLISH_PATH}",
                round_trip_ok=False,
                round_trip_problems=("remote node is opaque; round-trip not verifiable",),
            ),
            node or NetworkNode(),
        )

    node = node or NetworkNode()
    ack = node.publish(envelope)
    ok, problems = node.verify_round_trip(envelope)
    return (
        PublishResult(
            ack=ack,
            validation=validation,
            payload_bytes=payload_bytes,
            target="in-process NetworkNode stand-in",
            round_trip_ok=ok,
            round_trip_problems=tuple(problems),
        ),
        node,
    )


def _post(url: str, wire: dict) -> Ack:
    import httpx  # noqa: PLC0415

    endpoint = f"{url.rstrip('/')}{PUBLISH_PATH}"
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(endpoint, json=wire)
        resp.raise_for_status()
        data = resp.json() if resp.content else {}

    results = data.get("message", {}).get("catalogProcessingResults", [])
    statuses = {r.get("status") for r in results} or {"ACCEPTED"}
    status = "PARTIAL" if len(statuses) > 1 else next(iter(statuses))
    return Ack(
        status=status,
        catalog_ids=tuple(r.get("catalogId", "") for r in results),
        resources_indexed=sum(
            len(wire["message"]["catalogs"][i].get("resources", []))
            for i in range(len(wire["message"]["catalogs"]))
        ),
        attributes_bytes=0,
        message=json.dumps(data)[:400],
    )
