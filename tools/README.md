# tools/

## vocab_gaps.py — what did the vocabulary fail to recognise?

Optional, not part of the pipeline. Reads bulletins and lists terms they use
that `taxonomy/data/` does not know, ranked, with the source lines as evidence.

```bash
.venv/bin/python tools/vocab_gaps.py --all
.venv/bin/python tools/vocab_gaps.py data/imd_up_agromet.pdf --limit 40
.venv/bin/python tools/vocab_gaps.py --all --json gaps.json
```

The extraction report already says *how much* a bulletin resolved ("24.1% of
passages resolved to the taxonomy"). It does not say *what* it missed, so acting
on a low number today means re-reading the PDF by hand. This turns the
percentage into a triage list.

Deliberately does not call `extract()`. Extraction runs `detect_state()` first
and refuses a bulletin from an uncovered state — which is exactly when you most
need to know what is missing — so this works off `read_document()` alone and
runs on documents the pipeline will not publish. It applies the same
score-both-and-keep-the-winner encoding repair as `scenario1.repair_encoding()`,
so a mis-mapped Devanagari font does not get reported as missing vocabulary.

**It proposes, it does not decide.** Nothing is written to `taxonomy/data/` and
no subject URI is minted. Check the evidence lines before adding a term: a wrong
alias publishes a false coverage claim, and an alias must not be a whole word
inside another crop's name — see the header of `crops.json`.

Recall is measured rather than assumed. Five crops the Karnataka bulletin
plainly discusses were removed from `crops.json` and the scan re-run; all five
came back, at ranks 3, 4, 9, 11 and 30 of 272 candidates, so all inside the
default `--limit 30`. Precision is the looser half of the trade: roughly half
the top 30 is noise a reader dismisses in a second, while a missed crop silently
costs real coverage in the published catalogue.

## md2pdf.py — render the markdown docs to PDF

Optional, not part of the pipeline. Regenerates `docs_pdf/*.pdf` from the
`*.md` files in the package root.

```bash
# one-off venv, so doc tooling stays out of the project environment
python3.11 -m venv /tmp/pdfvenv
/tmp/pdfvenv/bin/pip install markdown weasyprint pygments
/tmp/pdfvenv/bin/python tools/md2pdf.py .
```

WeasyPrint needs pango/cairo/harfbuzz, which Homebrew already provides on this
machine (`brew list pango cairo harfbuzz`). Those native libraries are also what
make the inline Devanagari render — the CSS names "Devanagari Sangam MN" and
fontconfig resolves an Indic-capable face (observed: Mukta).

Note: Devanagari will NOT come back out of the PDF via text extraction, because
conjunct shaping leaves no reliable ToUnicode mapping. That is a property of
complex-script PDF text, not a rendering failure — check visually.
