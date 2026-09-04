# tools/

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
