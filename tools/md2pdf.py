"""Render the v4 markdown docs to PDF with WeasyPrint.

Font stack matters here: the docs quote Devanagari (प्रदेश) and Kannada (ಭತ್ತ)
inline, so the CSS names the macOS system fonts that actually cover those
scripts. Without that, those runs render as tofu boxes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import markdown
from weasyprint import CSS, HTML

CSS_TEXT = """
@page {
  size: A4;
  margin: 18mm 16mm 20mm 16mm;
  @bottom-center {
    content: counter(page) " / " counter(pages);
    font-family: "Helvetica Neue", Helvetica, sans-serif;
    font-size: 8pt; color: #888;
  }
}
/* Devanagari Sangam MN and Kannada Sangam MN carry the Indic scripts quoted
   in these documents; the Latin faces come first so body text is unaffected. */
body {
  font-family: "Helvetica Neue", Helvetica, Arial,
               "Devanagari Sangam MN", "Kannada Sangam MN", sans-serif;
  font-size: 9.5pt; line-height: 1.45; color: #1a1a1a; hyphens: none;
}
h1 { font-size: 19pt; margin: 0 0 6pt; color: #111; border-bottom: 2px solid #333;
     padding-bottom: 4pt; }
h2 { font-size: 13.5pt; margin: 16pt 0 5pt; color: #111;
     border-bottom: 1px solid #ccc; padding-bottom: 2pt; page-break-after: avoid; }
h3 { font-size: 11pt; margin: 12pt 0 4pt; color: #222; page-break-after: avoid; }
h4 { font-size: 10pt; margin: 10pt 0 3pt; color: #333; page-break-after: avoid; }
p  { margin: 4pt 0; }
strong { color: #000; }

code, kbd {
  font-family: "SF Mono", Menlo, Monaco, "Courier New", monospace;
  font-size: 8.2pt; background: #f2f2f4; padding: 0.5pt 2pt;
  border-radius: 2pt; color: #a01050;
}
pre {
  font-family: "SF Mono", Menlo, Monaco, "Courier New", monospace;
  font-size: 7.6pt; line-height: 1.32; background: #f7f7f9;
  border: 1px solid #e0e0e4; border-left: 3px solid #888;
  padding: 6pt 8pt; margin: 6pt 0; white-space: pre-wrap;
  word-wrap: break-word; page-break-inside: avoid;
}
pre code { background: none; padding: 0; color: #1a1a1a; font-size: 7.6pt; }

table {
  border-collapse: collapse; width: 100%; margin: 7pt 0;
  font-size: 8.2pt; page-break-inside: avoid;
}
th, td {
  border: 1px solid #ccc; padding: 3pt 5pt; text-align: left;
  vertical-align: top; word-wrap: break-word;
}
th { background: #eeeef1; font-weight: 600; }
tr:nth-child(even) td { background: #fafafb; }

blockquote {
  margin: 6pt 0; padding: 4pt 10pt; border-left: 3px solid #b0b0b8;
  background: #f7f7f9; color: #333;
}
ul, ol { margin: 4pt 0 4pt 0; padding-left: 16pt; }
li { margin: 2pt 0; }
hr { border: none; border-top: 1px solid #ddd; margin: 12pt 0; }
a { color: #0b57a4; text-decoration: none; }
"""


def convert(src: Path, dest: Path, title: str) -> None:
    text = src.read_text(encoding="utf-8")

    # The ASCII flow diagrams are inside fenced blocks already; nothing to do.
    # But bare-word autolinks like <name> would be eaten as HTML, so escape the
    # few angle-bracket placeholders that appear outside code fences.
    html_body = markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "codehilite", "sane_lists", "toc"],
        extension_configs={"codehilite": {"noclasses": True, "pygments_style": "friendly"}},
    )

    doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body>{html_body}</body></html>"""

    HTML(string=doc, base_url=str(src.parent)).write_pdf(
        dest, stylesheets=[CSS(string=CSS_TEXT)]
    )


def main() -> int:
    pkg = Path(sys.argv[1])
    out_dir = pkg / "docs_pdf"
    out_dir.mkdir(exist_ok=True)

    targets = sorted(pkg.glob("*.md"))
    if not targets:
        print(f"no .md files in {pkg}")
        return 1

    for src in targets:
        dest = out_dir / f"{src.stem}.pdf"
        convert(src, dest, src.stem)
        print(f"  {src.name:28s} -> docs_pdf/{dest.name:28s} {dest.stat().st_size:>9,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
