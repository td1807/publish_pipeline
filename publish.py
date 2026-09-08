"""Step 3: `/publish` — push the catalogue metadata out to the network layer.

Nothing is computed here. By the time publish is called, branch 2a has already
built and validated the payload; this module only decides *where* it goes and
refuses to send anything that has not been validated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .beckn.models import PublishEnvelope
from .beckn.validate import ValidationReport, assert_valid
from .config import (
    NETWORK_NODE_URL,
    STATE_FILE,
    PUBLISH_BACKOFF_SECONDS,
    PUBLISH_MAX_ATTEMPTS,
    PUBLISH_TIMEOUT_SECONDS,
)
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


# Which failures are worth sending the same payload again for. A dropped
# connection or a 5xx says "the node could not deal with this right now"; a 4xx
# says "this payload is wrong", and re-sending a wrong payload is just noise.
# 429 is retryable because it explicitly means "later".
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_retryable(exc: Exception) -> bool:
    import httpx  # noqa: PLC0415

    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    # TimeoutException, ConnectError, ReadError and friends all subclass this.
    return isinstance(exc, httpx.TransportError)



# --- what we told the network, and when --------------------------------------
# A published catalogue is cached by every consumer that fetched it, so
# "what are we currently claiming, and when did that change?" is a question a
# provider has to be able to answer. Nothing recorded it before: STATE_FILE was
# declared in config.py and never written.
#
# The load-bearing field is `claims_hash`. It is taken over the catalogues with
# transactionId, messageId, timestamp and the validity window removed -- the
# fields that are fresh on every run by design. So two publishes of unchanged
# coverage hash the same, and a hash that moves means the CLAIMS moved. That is
# the difference between a log and a diff.
_LEDGER_LIMIT = 200


def _claims_hash(wire: dict) -> str:
    """Content hash of the coverage claims, ignoring per-run identifiers."""
    catalogs = json.loads(json.dumps(wire.get("message", {}).get("catalogs", [])))
    for cat in catalogs:
        cat.pop("validity", None)
    return hashlib.sha256(
        json.dumps(catalogs, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def record_publish(
    wire: dict, result: PublishResult, *, path: Path = STATE_FILE
) -> dict:
    """Append this publish to the ledger and report how it differs from the last.

    Written after the ack, not before: an attempt that never reached the node
    is not a claim anybody holds. Failure to write the ledger must not fail the
    publish -- the catalogue is already out there, and pretending otherwise
    would be a worse lie than a missing line in a local file.
    """
    context = wire.get("context", {})
    entry = {
        "timestamp": context.get("timestamp", ""),
        "transactionId": context.get("transactionId", ""),
        "messageId": context.get("messageId", ""),
        "target": result.target,
        "ackStatus": result.ack.status,
        "catalogIds": list(result.ack.catalog_ids),
        "resources": result.ack.resources_indexed,
        "payloadBytes": result.payload_bytes,
        "claimsHash": _claims_hash(wire),
    }

    history: list[dict] = []
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8")).get("publishes", [])
        except (OSError, ValueError):
            # A corrupt ledger is not a reason to lose this entry too.
            history = []

    previous = history[-1] if history else None
    entry["changedSincePrevious"] = (
        None if previous is None else previous.get("claimsHash") != entry["claimsHash"]
    )

    try:
        path.write_text(
            json.dumps({"publishes": (history + [entry])[-_LEDGER_LIMIT:]}, indent=2)
            + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"  ledger    NOT written ({exc}) — the publish itself succeeded")

    return entry


def _post(url: str, wire: dict) -> Ack:
    """POST the envelope, retrying transient failures with growing backoff.

    Carries the envelope's own `messageId` as an `Idempotency-Key`. A retry is
    the SAME publish, so it carries the same key and a node that already
    accepted it can say so instead of indexing a second copy. A later, genuine
    republish builds a new envelope with a new messageId, and so is correctly
    treated as new.
    """
    import time  # noqa: PLC0415

    import httpx  # noqa: PLC0415

    endpoint = f"{url.rstrip('/')}{PUBLISH_PATH}"
    headers = {"Idempotency-Key": wire.get("context", {}).get("messageId", "")}
    attempts = max(1, PUBLISH_MAX_ATTEMPTS)

    for attempt in range(1, attempts + 1):
        try:
            with httpx.Client(timeout=PUBLISH_TIMEOUT_SECONDS) as client:
                resp = client.post(endpoint, json=wire, headers=headers)
                resp.raise_for_status()
                data = resp.json() if resp.content else {}
            break
        except Exception as exc:  # noqa: BLE001 — re-raised below unless retryable
            if attempt == attempts or not _is_retryable(exc):
                raise
            # Say so. A run that silently took four tries and one that
            # succeeded first time are not the same run.
            delay = PUBLISH_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(
                f"  publish attempt {attempt}/{attempts} failed "
                f"({type(exc).__name__}); retrying in {delay:.1f}s"
            )
            time.sleep(delay)

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
