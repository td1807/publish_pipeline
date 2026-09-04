"""Runnable, narrated scenario 1.

    .venv/bin/python main.py --all --fresh
    .venv/bin/python main.py --pdf imd_karnataka_agromet.pdf
    .venv/bin/python main.py --pdf imd_karnataka_district_kannada.pdf   # refused: a scan

Prints what each step did, with real numbers, and writes the actual publish
payload to evidence/ so the walkthrough can be read without running anything.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import DATA_DIR, EVIDENCE_DIR, Settings
from .ingest.pdf_text import UnusableDocument
from .scenario1 import onboard_all, publish_all
from .taxonomy.vocab import load_vocabulary
from .vectors.embeddings import get_embedder
from .vectors.store import VectorIndex

DEFAULT_PDFS = [
    "imd_karnataka_agromet.pdf",
    "imd_up_agromet.pdf",
    "imd_rajasthan_agromet.pdf",
]

# One question, asked only to prove branch 2b's index is usable later. Scenario 1
# itself involves no question -- this is a smoke check, not the consumer flow.
SMOKE_QUERY = "pigeon pea flowers dropping, what should I spray"


def _rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Scenario 1: onboard PDFs, publish to Beckn")
    ap.add_argument("--pdf", action="append", default=[], help="filename in data/ or a path")
    ap.add_argument("--all", action="store_true", help="run all three state bulletins")
    ap.add_argument("--fresh", action="store_true", help="recreate the vector collection")
    ap.add_argument("--no-save", action="store_true", help="do not write evidence/")
    ap.add_argument(
        "--show-resource",
        metavar="SUBSTRING",
        help="print one resource's full resourceAttributes to stdout, "
        "matched by substring of its id (e.g. 'koppal')",
    )
    args = ap.parse_args(argv)

    names = args.pdf or (DEFAULT_PDFS if args.all else DEFAULT_PDFS[:1])
    paths = [str(p if (p := Path(n)).exists() else DATA_DIR / n) for n in names]

    _rule("configuration")
    print(Settings().describe())

    vocab = load_vocabulary()
    print(
        f"vocabulary    {len(vocab.subject_by_slug)} subjects, "
        f"{sum(len(set(d.values())) for d in vocab.districts.values())} districts "
        f"across {len(vocab.states)} states, {len(set(vocab.topics.values()))} topics"
    )

    _rule("step 1 + 2 — ingest, then branch 2a and 2b in parallel")
    embedder = get_embedder()
    index = VectorIndex(embedder)
    print(index.describe(), "\n")

    onboardings = []
    refused: list[tuple[str, str]] = []
    for path in paths:
        try:
            done, index = onboard_all([path], index=index, fresh=args.fresh, vocab=vocab)
        except UnusableDocument as exc:
            # The refusal path, for both kinds of document we will not stand
            # behind: a scan we cannot read, and a state the vocabulary does
            # not cover. Either publishes nothing at all, rather than a
            # resource claiming coverage we never read or never had.
            refused.append((Path(path).name, str(exc)))
            print(f"\nREFUSED  {Path(path).name}\n         {exc}")
            continue
        args.fresh = False  # only the first document recreates the collection
        onboardings.extend(done)
        print(done[0].summary())
        for w in done[0].extraction.warnings:
            print(f"  ! {w}")
        print()

    if not onboardings:
        print("\nNothing was publishable. Exiting without a publish call.")
        return 1

    _rule("step 3 — /catalog/publish to the network layer")
    envelope, result, node = publish_all(onboardings)
    print(result.summary())

    print("\nwhat the network layer now holds (resourceAttributes only):")
    for k, v in node.facet_index().items():
        print(f"  {k:20s} {v}")

    # The catalogue-carries-no-prose invariant, checked against real advisory
    # text taken from the Karnataka bulletin rather than a synthetic string.
    for needle in ("NAA", "moisture stress", "ml/litre"):
        leaked = node.contains_text(needle)
        print(f"  advisory text {needle!r:18s} present in network layer: {leaked}")
        if leaked:
            print("  ^ INVARIANT VIOLATED: prose reached the catalogue")
            return 1

    _rule("branch 2b is usable later — retrieval smoke check")
    print(f"query: {SMOKE_QUERY!r}")
    hits = index.search(SMOKE_QUERY, limit=3)
    if not hits:
        print("  no hits — the index is not answering")
        return 1
    for h in hits:
        flag = "" if h.semantic else "   [semantic=False: spellings only, not meanings]"
        print(f"  {h.score:.3f}  {h.citation:38s} {h.resource_id}{flag}")
        print(f"         {h.text[:150].replace(chr(10), ' ')}...")

    _rule("totals")
    print(f"documents onboarded   {len(onboardings)}")
    print(f"documents refused     {len(refused)}")
    print(f"passages extracted    {sum(len(o.passages) for o in onboardings)}")
    print(f"resources published   {sum(len(o.catalog.resources) for o in onboardings)}")
    print(f"vectors indexed       {index.count()}")
    print(f"publish payload       {result.payload_bytes:,} bytes")
    print(
        "branch timings        "
        + " | ".join(
            f"{o.document.path.name.removeprefix('imd_').removesuffix('_agromet.pdf')}: "
            f"2a {o.timing.branch_2a_seconds:.2f}s / 2b {o.timing.branch_2b_seconds:.2f}s "
            f"/ wall {o.timing.wall_seconds:.2f}s"
            for o in onboardings
        )
    )

    if args.show_resource:
        _rule(f"resourceAttributes matching {args.show_resource!r}")
        needle = args.show_resource.lower()
        found = 0
        for o in onboardings:
            for res in o.catalog.resources:
                if needle not in res.id.lower():
                    continue
                found += 1
                print(f"\n--- {res.id}   [{o.document.path.name}] ---")
                print(json.dumps(
                    res.resourceAttributes.model_dump(by_alias=True, exclude_none=True),
                    ensure_ascii=False, indent=2,
                ))
        if not found:
            print(f"no resource id contains {args.show_resource!r}")

    if not args.no_save:
        EVIDENCE_DIR.mkdir(exist_ok=True)
        payload = EVIDENCE_DIR / "publish_payload.json"
        payload.write_text(
            json.dumps(envelope.to_wire(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nwrote evidence/{payload.name} ({payload.stat().st_size:,} bytes)"
              "  — the combined /catalog/publish body")

        # One file per PDF as well. The combined envelope is what goes on the
        # wire, but for review "what did THIS bulletin claim?" is the question
        # people actually ask, and scrolling three states to answer it is
        # needless friction.
        per_doc = EVIDENCE_DIR / "resources"
        per_doc.mkdir(exist_ok=True)
        print("\nper-document resourceAttributes:")
        for o in onboardings:
            stem = o.document.path.stem.removeprefix("imd_").removesuffix("_agromet")
            dest = per_doc / f"{stem}.json"
            dest.write_text(
                json.dumps(o.resources_json(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(
                f"  evidence/resources/{dest.name:16s} "
                f"{o.state_code}  {len(o.catalog.resources):>3} resources  "
                f"{dest.stat().st_size:>8,} bytes"
            )

    index.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
