"""Step 1: a document -> pages of text. And the cases where it refuses.

PDF is what the IMD bulletins arrive as, and what the extractors are tuned
for, but nothing downstream of this module knows that: everything past here
consumes pages of text with numbers on them. MuPDF reads DOCX, TXT, HTML,
EPUB and XPS as well, so those work too, and are covered by tests.

Two documents are refused rather than published. One is a format no
available extractor can open -- the legacy .doc binary, a spreadsheet, an
archive. The other is a scan: a scanned bulletin has no text layer. Extracting it yields a handful of stray
characters, which would sail through the rest of the pipeline and publish a
resource claiming to cover a district whose advisories we never actually read.
That refusal is the feature, not a limitation: run OCR first, then re-ingest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# A page needs at least this much text to count as having a text layer at all.
MIN_CHARS_PER_PAGE = 40
# And at least this share of pages must clear that bar for the document to be
# usable. One cover page of pure imagery is normal; forty of them is a scan.
MIN_USABLE_PAGE_RATIO = 0.5


class UnusableDocument(RuntimeError):
    """Raised instead of publishing a catalogue we cannot stand behind."""


@dataclass(frozen=True)
class Page:
    number: int  # 1-based, as a citation would print it
    text: str


@dataclass(frozen=True)
class Document:
    path: Path
    pages: tuple[Page, ...]
    extractor: str

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for p in self.pages)

    @property
    def page_count(self) -> int:
        return len(self.pages)


def read_document(path: str | Path) -> Document:
    """Extract text with pymupdf, falling back to pdfplumber.

    pymupdf keeps table columns in a readable order more often than the
    alternatives, which matters a great deal here: the entire crop advisory
    payload of an IMD bulletin lives inside
    `Major crops | Stage | Pest/disease | Agricultural Advisories` tables.

    A format neither extractor can open is refused, not raised as whatever
    exception the library happened to throw. Both outcomes are the same answer
    -- this document cannot be published -- so both are UnusableDocument, and a
    caller running several documents can report one and continue with the rest.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    problems: list[str] = []
    for attempt in (_extract_pymupdf, _extract_pdfplumber):
        pages, extractor, problem = attempt(path)
        if pages is not None:
            break
        problems.append(problem)
    else:
        raise UnusableDocument(
            f"Refusing to publish a catalogue from an unreadable document: "
            f"{path.name} could not be opened ({'; '.join(problems)}). "
            "PDF and DOCX are what these bulletins arrive as; TXT, HTML, EPUB "
            "and XPS also read. The legacy .doc binary format, spreadsheets and "
            "archives do not — convert to PDF or DOCX and re-ingest."
        )

    usable = [p for p in pages if len(p.text.strip()) >= MIN_CHARS_PER_PAGE]
    ratio = len(usable) / max(len(pages), 1)
    if not usable or ratio < MIN_USABLE_PAGE_RATIO:
        raise UnusableDocument(
            f"Refusing to publish a catalogue from an unusable document: "
            f"{path.name} has {len(usable)} of {len(pages)} pages with a text "
            f"layer ({ratio:.0%}, need {MIN_USABLE_PAGE_RATIO:.0%}). "
            "This looks like a scan. Run OCR and re-ingest."
        )

    return Document(path=path, pages=tuple(pages), extractor=extractor)


# Each extractor returns (pages, extractor name, why it could not be used).
# The exceptions are caught broadly on purpose: MuPDF alone raises FileDataError,
# FzErrorFormat and zip errors depending on how a file is malformed, and naming
# those types here would couple this module to the internals of a dependency
# that is probed at runtime precisely so it can be absent.
def _extract_pymupdf(path: Path) -> tuple[list[Page] | None, str, str]:
    try:
        import pymupdf  # noqa: PLC0415  (optional dependency, probed at runtime)
    except ImportError:
        return None, "", "pymupdf is not installed"
    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001  (see the note above)
        return None, "", f"pymupdf could not open it: {exc}"
    try:
        pages = [
            Page(number=i + 1, text=_tidy(doc[i].get_text()))
            for i in range(doc.page_count)
        ]
    finally:
        doc.close()
    return pages, "pymupdf", ""


def _extract_pdfplumber(path: Path) -> tuple[list[Page] | None, str, str]:
    try:
        import pdfplumber  # noqa: PLC0415
    except ImportError:
        return None, "", "pdfplumber is not installed"
    try:
        with pdfplumber.open(path) as doc:
            pages = [
                Page(number=i + 1, text=_tidy(page.extract_text() or ""))
                for i, page in enumerate(doc.pages)
            ]
    except Exception as exc:  # noqa: BLE001  (PDF-only, so anything else lands here)
        return None, "", f"pdfplumber could not open it: {exc}"
    return pages, "pdfplumber", ""


_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")
# The IMD bulletins bullet their advisories with these, one per advisory line.
_BULLETS = re.compile(r"^[\s•●▪\-\*→]+", re.M)


def _tidy(text: str) -> str:
    """Collapse extraction noise without moving any content across lines.

    Deliberately conservative: page and line structure is provenance. A chunk
    that claims page 14 has to actually be on page 14, because a follow-up
    answer cites that number to a farmer.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _WS.sub(" ", text)
    text = _BULLETS.sub("", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()
