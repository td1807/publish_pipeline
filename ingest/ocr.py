"""Recover text from pages that are pictures of text.

The pipeline reads a text layer. A page that is an image has no text layer, so
everything downstream sees an empty page -- and an empty page cannot contradict
anything, which is why a partially-scanned bulletin publishes thin coverage
rather than failing loudly.

The Rajasthan bulletin is exactly that shape. 23 of its 30 pages carry a
district heading in text and the entire advisory table as an image. Measured on
the bundled file: 14,332 extractable characters become 59,661 with OCR, and the
crops visible to the taxonomy go from 8 to 26 -- every one of which was already
in crops.json. The vocabulary was never the limit; the text simply never
reached it.

OFF BY DEFAULT. Set OCR_ENABLED=1 or pass --ocr. Two reasons: the numbers in
evidence/ were produced without it and must stay reproducible, and running the
same bulletin with and without OCR is a better demonstration than either alone.

WHAT THIS DOES NOT DO
---------------------
It does not decide that OCR output is correct. Tesseract on Devanagari damages
exactly the tokens that matter most in an agricultural advisory: on page 9 of
the Rajasthan bulletin `क्विनालफॉस 25 EC (1 लीटर/हेक्टेयर)` came back as
`गस 25 50 (। लीटर/हेक्टेयर)`. A corrupted pesticide dose handed to a farmer is
a real harm, so passages recovered this way are flagged (`from_ocr`) in the
vector payload and the caller decides what to do with them.

The catalogue needs no such flag. Branch 2a publishes crop names, district
names and topics, all matched against a closed vocabulary -- OCR noise either
resolves to a subject that exists or resolves to nothing, and it can never mint
one. Dosages are never published at all. So 2a can take OCR text as-is; 2b is
the branch that has to be careful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..config import (
    OCR_DPI,
    OCR_LANGS,
    OCR_MIN_TOPICS,
    OCR_MIN_CHARS,
    OCR_PAGE_TEXT_FLOOR,
)

if TYPE_CHECKING:  # pragma: no cover
    from ..taxonomy.vocab import Vocabulary
    from .document_text import Document


@dataclass(frozen=True)
class OcrReading:
    """What OCR was asked to do, and what it was allowed to keep."""

    available: bool
    reason: str = ""
    pages_attempted: int = 0
    pages_accepted: int = 0
    # Distinct from "refused". A refused page was read and judged not to help;
    # an errored page never produced a reading at all. Collapsing the two hides
    # bugs behind a message that sounds like a safeguard doing its job.
    pages_errored: int = 0
    chars_before: int = 0
    chars_after: int = 0
    terms_before: int = 0
    terms_after: int = 0

    @property
    def applied(self) -> bool:
        return self.pages_accepted > 0

    def summary(self) -> str:
        if not self.available:
            return f"OCR unavailable — {self.reason}"
        if not self.pages_attempted:
            return "OCR not needed — every page already has a text layer"
        if self.pages_errored:
            err = (
                f" · {self.pages_errored} page(s) FAILED to process"
                f" ({self.reason})"
            )
        else:
            err = ""
        if not self.pages_accepted:
            return (
                f"OCR ran on {self.pages_attempted} page(s), kept none — no page "
                f"gained vocabulary terms{err}"
            )
        return (
            f"OCR recovered {self.pages_accepted}/{self.pages_attempted} page(s) — "
            f"{self.chars_before:,} → {self.chars_after:,} chars, "
            f"{self.terms_before} → {self.terms_after} known terms{err}"
        )


def ocr_available() -> tuple[bool, str]:
    """Probe rather than assume. Both halves have to be present."""
    try:
        import pytesseract  # noqa: PLC0415
    except ImportError:
        return False, "pytesseract is not installed (`pip install pytesseract`)"
    try:
        pytesseract.get_tesseract_version()
    except Exception as exc:  # noqa: BLE001 — pytesseract raises its own type
        return False, f"the tesseract binary is not on PATH ({exc})"
    return True, ""


def _known_terms(text: str, vocab: Vocabulary) -> int:
    """How many crop, livestock and district names the vocabulary can see.

    Deliberately script-agnostic, unlike `language.score_terms()`, which counts
    Devanagari only. A scanned Kannada bulletin has to be scoreable too.
    """
    if not text.strip():
        return 0
    n = len({s.slug for s in vocab.subjects_in(text)})
    for state in vocab.districts:
        n += len({d.name for d in vocab.districts_in(text, state)})
    return n


def _page_is_a_picture(page_text: str, has_images: bool) -> bool:
    """A heading in text with the content as an image still counts as a picture.

    The scan gate in document_text.py asks whether a page has ANY text. That is
    the right question for a fully scanned file and the wrong one here: these
    pages carry `बज़ला:जालोर` (10 characters) above an advisory table that is a
    JPEG, so they pass a presence check while being unreadable in substance.
    """
    return has_images and len(page_text.strip()) < OCR_PAGE_TEXT_FLOOR


def augment_with_ocr(
    doc: Document,
    vocab: Vocabulary,
    *,
    dpi: int = OCR_DPI,
    langs: str = OCR_LANGS,
) -> tuple[Document, OcrReading]:
    """Add recovered text to image pages, keeping only what demonstrably helps.

    The accept/reject gate is the one `scenario1.repair_encoding()` already
    uses for the Devanagari font defect: run the transform, score both versions
    against the vocabulary, keep the result only if more known terms match.
    A page where OCR returns noise scores no better and is discarded, so a bad
    OCR pass degrades to no OCR pass rather than to a polluted index.

    Recovered text is APPENDED below the existing text rather than replacing
    it. The district heading in the text layer is correct and OCR may mangle
    it, and `passages.py` tracks the current district by reading the top of the
    page -- so the good heading has to stay where it is.
    """
    from .document_text import Document as Doc, Page  # noqa: PLC0415

    ok, reason = ocr_available()
    if not ok:
        return doc, OcrReading(available=False, reason=reason)

    try:
        import io  # noqa: PLC0415

        import pymupdf  # noqa: PLC0415
        import pytesseract  # noqa: PLC0415
        from PIL import Image  # noqa: PLC0415  (a pytesseract dependency)
    except ImportError as exc:  # pragma: no cover
        return doc, OcrReading(available=False, reason=str(exc))

    if doc.path.suffix.lower() != ".pdf":
        return doc, OcrReading(
            available=True, reason="only PDF pages can be rasterised"
        )

    src = pymupdf.open(doc.path)
    try:
        attempted = accepted = errors = 0
        first_error = ""
        chars_before = chars_after = 0
        terms_before = terms_after = 0
        new_pages: list[Page] = []

        for page in doc.pages:
            original = page.text
            chars_before += len(original.strip())
            before = _known_terms(original, vocab)
            terms_before += before

            index = page.number - 1
            has_images = 0 <= index < src.page_count and bool(src[index].get_images())

            if not _page_is_a_picture(original, has_images):
                new_pages.append(page)
                chars_after += len(original.strip())
                terms_after += before
                continue

            attempted += 1
            try:
                pix = src[index].get_pixmap(dpi=dpi)
                # pytesseract wants a PIL image or a path, never raw bytes.
                # Handing it bytes raises TypeError, and an earlier version of
                # this function caught that broadly and reported it as a page
                # the gate had REFUSED — a crash wearing the costume of a
                # working safeguard. Hence `errors` below: a page that failed
                # to process is counted separately from a page whose OCR was
                # judged and rejected, because only one of those is a bug.
                recovered = pytesseract.image_to_string(
                    Image.open(io.BytesIO(pix.tobytes("png"))), lang=langs
                )
            except Exception as exc:  # noqa: BLE001 — a page that will not
                # render is not a reason to abandon the document.
                errors += 1
                if not first_error:
                    first_error = f"{type(exc).__name__}: {exc}"
                new_pages.append(page)
                chars_after += len(original.strip())
                terms_after += before
                continue

            merged = f"{original}\n{recovered}".strip()
            after = _known_terms(merged, vocab)

            # Three conditions, all required.
            #   more known terms  — the OCR said something the taxonomy knows
            #   enough characters — one lucky match on a page of speckle is not
            #                       a recovered table
            #   names topics      — the one that actually separates a recovered
            #                       advisory from a mangled forecast grid. A
            #                       page worth keeping gives ADVICE. See the
            #                       note on OCR_MIN_TOPICS in config.py for the
            #                       heuristics that failed before this one.
            if (
                after > before
                and len(recovered.strip()) >= OCR_MIN_CHARS
                and len(vocab.topics_in(recovered)) >= OCR_MIN_TOPICS
            ):
                accepted += 1
                new_pages.append(Page(number=page.number, text=merged, ocr=True))
                chars_after += len(merged)
                terms_after += after
            else:
                new_pages.append(page)
                chars_after += len(original.strip())
                terms_after += before
    finally:
        src.close()

    return (
        Doc(path=doc.path, pages=tuple(new_pages), extractor=doc.extractor),
        OcrReading(
            available=True,
            pages_attempted=attempted,
            pages_accepted=accepted,
            pages_errored=errors,
            reason=first_error,
            chars_before=chars_before,
            chars_after=chars_after,
            terms_before=terms_before,
            terms_after=terms_after,
        ),
    )
