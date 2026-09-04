"""Scenario 1: a provider onboards a PDF. Nobody has asked anything yet.

    step 1   ingest the PDF, extract passages ONCE
    step 2   in parallel:
               2a  passages -> Beckn catalogue metadata
               2b  passages -> embeddings -> vector DB
    step 3   /publish the catalogue to the network layer

The two branches of step 2 are independent of any question, and they stay
independent of each other: 2a produces a small index card for the network, 2b
fills the provider's own shelf so a later follow-up can be answered. Neither
waits on the other, which is why they run concurrently here rather than in
sequence.

Threads, not asyncio: branch 2b is CPU-bound inside torch, which releases the
GIL during its matmuls, so a thread genuinely overlaps with 2a's work. The
timings reported below are measured per branch and against wall clock, so
"they ran in parallel" is a measurement rather than a claim.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .beckn.catalog import build_catalog
from .beckn.envelope import build_envelope
from .beckn.models import Catalog, PublishEnvelope
from .config import DOMAIN, PROVIDER_ID
from .ingest.passages import ExtractionReport, Passage, extract, detect_state
from .ingest.document_text import Document, read_document
from .publish import PublishResult, publish
from .taxonomy.vocab import Vocabulary, load_vocabulary
from .vectors.embeddings import Embedder, TokenReport, get_embedder
from .vectors.store import IndexResult, VectorIndex


@dataclass
class BranchTiming:
    branch_2a_seconds: float
    branch_2b_seconds: float
    wall_seconds: float

    @property
    def saved_seconds(self) -> float:
        return (self.branch_2a_seconds + self.branch_2b_seconds) - self.wall_seconds

    @property
    def ran_in_parallel(self) -> bool:
        # Wall clock materially below the serial sum is the only honest evidence
        # of overlap. A 5% margin keeps scheduling noise from claiming a win.
        return self.wall_seconds < (self.branch_2a_seconds + self.branch_2b_seconds) * 0.95

    @property
    def ratio(self) -> float:
        """How lopsided the two branches are."""
        return self.branch_2b_seconds / max(self.branch_2a_seconds, 1e-9)

    def summary(self) -> str:
        # Be precise about what concurrency did and did not buy. The branches
        # genuinely run at the same time, but 2b (a 560M-parameter model) costs
        # orders of magnitude more than 2a (dict building), so wall clock is
        # just 2b's time and the measured saving is ~0. Reporting a speed-up
        # here would be a lie by rounding; the reason to run them concurrently
        # is architectural — neither branch blocks or can corrupt the other —
        # not throughput. See STEP2_WALKTHROUGH.md.
        if self.ran_in_parallel:
            verdict = f"overlapped, saved {self.saved_seconds:.2f}s"
        else:
            verdict = (
                f"no measurable saving — 2b is {self.ratio:,.0f}x slower than 2a, "
                "so wall clock is simply 2b"
            )
        return (
            f"2a {self.branch_2a_seconds:.3f}s · 2b {self.branch_2b_seconds:.3f}s · "
            f"wall {self.wall_seconds:.3f}s — {verdict}"
        )


@dataclass
class Onboarding:
    """Everything one PDF produced. Deliberately keeps both branches' outputs."""

    document: Document
    passages: list[Passage]
    extraction: ExtractionReport
    state_code: str
    state_name: str
    catalog: Catalog          # branch 2a
    index_result: IndexResult # branch 2b
    tokens: TokenReport | None
    timing: BranchTiming

    def resources_json(self) -> dict:
        """This document's resources and their full resourceAttributes.

        One file per PDF is far more readable than the combined publish
        envelope: a reviewer wants to see what *this* bulletin claimed, not
        scroll past two other states to find it. The `resources` array is
        exactly what went on the wire — same objects, same order.
        """
        from collections import Counter

        types = Counter(
            r.resourceAttributes.type.split(":")[-1]
            for r in self.catalog.resources
            if r.resourceAttributes
        )
        return {
            "document": self.document.path.name,
            "state": {"code": self.state_code, "name": self.state_name},
            "catalogId": self.catalog.id,
            "provider": self.catalog.provider.model_dump(exclude_none=True),
            "extraction": {
                "pages": self.extraction.pages,
                "passages": self.extraction.passages,
                "subjectResolution": round(self.extraction.subject_resolution, 4),
                "districtsResolved": self.extraction.districts,
                "languages": self.extraction.languages,
                "categories": self.extraction.categories,
                "warnings": list(self.extraction.warnings),
            },
            "resourceCount": len(self.catalog.resources),
            "capabilityTypes": dict(sorted(types.items())),
            "resources": [
                {
                    "id": r.id,
                    "descriptor": r.descriptor.model_dump(exclude_none=True)
                    if r.descriptor
                    else None,
                    "resourceAttributes": r.resourceAttributes.model_dump(
                        by_alias=True, exclude_none=True
                    )
                    if r.resourceAttributes
                    else None,
                }
                for r in self.catalog.resources
            ],
        }

    def summary(self) -> str:
        from collections import Counter

        types = Counter(
            r.resourceAttributes.type.split(":")[-1]
            for r in self.catalog.resources
            if r.resourceAttributes
        )
        type_lines = "\n".join(
            f"                {n:>4}  {t}" for t, n in sorted(types.items())
        )
        tok = f"\n  2b tokens   {self.tokens.summary()}" if self.tokens else ""
        return (
            f"{self.extraction.summary()}\n"
            f"  2a          {len(self.catalog.resources)} resources across "
            f"{len(types)} capability types\n{type_lines}\n"
            f"  2b          {self.index_result.points} vectors, "
            f"{self.index_result.dim}d, cosine{tok}\n"
            f"  parallel    {self.timing.summary()}"
        )


def onboard(
    doc_path: str,
    *,
    index: VectorIndex,
    vocab: Vocabulary | None = None,
    provider_id: str = PROVIDER_ID,
    domain: str = DOMAIN,
) -> Onboarding:
    """Steps 1 and 2 for a single document."""
    vocab = vocab or load_vocabulary()

    # --- step 1 -------------------------------------------------------------
    doc = read_document(doc_path)
    state_code = detect_state(doc, vocab)
    state_name = vocab.state(state_code).name

    # ONE extraction pass. Both branches consume this same list; see
    # ingest/passages.py for why that is not negotiable.
    passages, report = extract(doc, provider_id=provider_id, domain=domain, vocab=vocab)
    if not passages:
        raise RuntimeError(f"no passages extracted from {doc.path.name}")

    # --- step 2, both branches at once --------------------------------------
    timings: dict[str, float] = {}

    def branch_2a() -> Catalog:
        start = time.perf_counter()
        try:
            return build_catalog(
                passages,
                state_code=state_code,
                state_name=state_name,
                provider_id=provider_id,
                domain=domain,
                vocab=vocab,
            )
        finally:
            timings["2a"] = time.perf_counter() - start

    def branch_2b() -> tuple[IndexResult, TokenReport | None]:
        start = time.perf_counter()
        try:
            tokens = index.embedder.token_report([p.text for p in passages])
            return index.index_passages(passages), tokens
        finally:
            timings["2b"] = time.perf_counter() - start

    wall_start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="step2") as pool:
        fut_a = pool.submit(branch_2a)
        fut_b = pool.submit(branch_2b)
        catalog = fut_a.result()
        index_result, tokens = fut_b.result()
    wall = time.perf_counter() - wall_start

    return Onboarding(
        document=doc,
        passages=passages,
        extraction=report,
        state_code=state_code,
        state_name=state_name,
        catalog=catalog,
        index_result=index_result,
        tokens=tokens,
        timing=BranchTiming(
            branch_2a_seconds=timings.get("2a", 0.0),
            branch_2b_seconds=timings.get("2b", 0.0),
            wall_seconds=wall,
        ),
    )


def onboard_all(
    doc_paths: list[str],
    *,
    embedder: Embedder | None = None,
    index: VectorIndex | None = None,
    fresh: bool = False,
    vocab: Vocabulary | None = None,
) -> tuple[list[Onboarding], VectorIndex]:
    vocab = vocab or load_vocabulary()
    if index is None:
        index = VectorIndex(embedder or get_embedder())
    index.ensure_collection(recreate=fresh)

    return [onboard(p, index=index, vocab=vocab) for p in doc_paths], index


def publish_all(
    onboardings: list[Onboarding],
) -> tuple[PublishEnvelope, PublishResult, "object"]:
    """Step 3: one envelope carrying every catalogue, published together."""
    envelope = build_envelope([o.catalog for o in onboardings])
    result, node = publish(envelope)
    return envelope, result, node
