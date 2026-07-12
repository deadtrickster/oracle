# /// script
# requires-python = ">=3.10"
# dependencies = ["beautifulsoup4", "lxml", "markdownify"]
# ///
"""cppreference offline HTML -> clean per-page markdown, mirroring sanitize-apidocs.py.

cppreference is a MediaWiki dump; the article body is in #mw-content-text. Extract that,
drop the nav/edit/category cruft, markdownify. C and C++ reference only (skip other langs).

  uv run sanitize-cppref.py
  corpus/cpp/reference/en/{c,cpp}/**/*.html  ->  corpus/cpp/md/**/*.md
"""
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify

SRC = Path("corpus/cpp/reference/en")
OUT = Path("corpus/cpp/md")
LANGS = {"c", "cpp"}
STRIP = [".editsection", ".mw-editsection", ".t-navbar", "#toc", ".toc", ".printfooter",
         ".catlinks", ".noprint", "#siteSub", "#contentSub", ".mw-jump-link",
         "#jump-to-nav", ".t-nv-begin", "#cpp-navigation"]


def convert(html: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one("#mw-content-text")
    if main is None:
        return None
    for sel in STRIP:
        for node in main.select(sel):
            node.decompose()
    return markdownify(str(main), heading_style="ATX")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    n = skipped = 0
    for html_file in SRC.rglob("*.html"):
        rel = html_file.relative_to(SRC)
        if not rel.parts or rel.parts[0] not in LANGS:
            continue
        try:
            md = convert(html_file.read_text(errors="ignore"))
        except Exception:
            md = None
        if not md or len(md.strip()) < 60:
            skipped += 1
            continue
        out = OUT / rel.with_suffix(".md")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(md)
        n += 1
        if n % 500 == 0:
            print(f"  {n} pages...", flush=True)
    print(f"done: {n} cppreference C/C++ pages -> markdown ({skipped} skipped)")


if __name__ == "__main__":
    main()
