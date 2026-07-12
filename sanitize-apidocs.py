# /// script
# requires-python = ">=3.10"
# dependencies = ["beautifulsoup4", "markdownify", "lxml"]
# ///
"""PLAN.md Step 3c: rustdoc/mdBook HTML -> clean markdown for RAG ingestion.

  uv run sanitize-apidocs.py rustdoc <crate-root>... -o <outdir>
      one merged .md per module (crate-root = dir holding index.html, e.g. rustup-html/std)
  uv run sanitize-apidocs.py mdbook  <book-dir|html-file>... -o <outdir>
      one .md per book (uses print.html when present) or per given file
"""
import argparse
import re
import sys
from multiprocessing import Pool
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify

# rustdoc item pages worth keeping; everything else in a module dir is chrome
ITEM_PREFIXES = ("struct.", "enum.", "trait.", "fn.", "macro.", "constant.",
                 "type.", "union.", "primitive.", "keyword.", "attr.", "derive.",
                 "static.")
SKIP_DIRS = {"src", "static.files", "implementors", "trait.impl", "type.impl"}
NOISE_SELECTORS = [
    "script", "style", "noscript", "button", "svg",
    "a.src", "a.anchor", "span.since-right", "rustdoc-search",
    ".sidebar", ".mobile-topbar", ".search-form", ".sub", "#copy-path",
    ".collapse-toggle", ".out-of-band", ".rightside",
]


def html_to_md(html: str, container: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one(container)
    if main is None:
        return None
    for sel in NOISE_SELECTORS:
        for node in main.select(sel):
            node.decompose()
    md = markdownify(str(main), heading_style="ATX", code_language="rust")
    md = re.sub(r"\[§\]\([^)]*\)", "", md)            # heading anchor links
    md = re.sub(r"^Expand description$", "", md, flags=re.M)
    md = re.sub(r"\n{3,}", "\n\n", md)  # collapse blank-line runs
    return md.strip()


def is_redirect_stub(text: str) -> bool:
    return len(text) < 1200 and "http-equiv=\"refresh\"" in text


# ---- rustdoc: merge item pages per module ------------------------------------

def convert_module(job: tuple[str, str, str]) -> tuple[str, int, str]:
    """(module_dir, crate_root, outdir) -> (relpath, pages_converted, error)"""
    mod_dir, crate_root, outdir = (Path(p) for p in job)
    rel = mod_dir.relative_to(crate_root.parent)  # e.g. std/vec
    parts, err = [], ""
    pages = sorted(mod_dir.glob("*.html"),
                   key=lambda p: (p.name != "index.html", p.name))
    for page in pages:
        name = page.name
        if name != "index.html" and not name.startswith(ITEM_PREFIXES):
            continue
        try:
            text = page.read_text(errors="ignore")
            if is_redirect_stub(text):
                continue
            md = html_to_md(text, "#main-content")
            if md:
                title = "module index" if name == "index.html" else name[:-5]
                parts.append(f"# {'::'.join(rel.parts)} — {title}\n\n{md}")
        except Exception as e:  # keep going; one bad page shouldn't kill a module
            err = f"{page}: {e}"
    if parts:
        out = Path(outdir) / rel.parent / f"{rel.name}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n\n---\n\n".join(parts))
    return (str(rel), len(parts), err)


def run_rustdoc(roots: list[Path], outdir: Path, jobs: int) -> None:
    work = []
    for root in roots:
        if not (root / "index.html").exists():
            print(f"  ! {root}: no index.html, skipping", file=sys.stderr)
            continue
        for d in [root, *sorted(root.rglob("*/"))]:
            d = Path(d)
            if any(part in SKIP_DIRS for part in d.relative_to(root.parent).parts):
                continue
            if any(f.name == "index.html" or f.name.startswith(ITEM_PREFIXES)
                   for f in d.glob("*.html")):
                work.append((str(d), str(root), str(outdir)))
    print(f"rustdoc: {len(work)} modules across {len(roots)} crate roots")
    done = pages = 0
    with Pool(jobs) as pool:
        for rel, n, err in pool.imap_unordered(convert_module, work, chunksize=4):
            done += 1
            pages += n
            if err:
                print(f"  ! {err}", file=sys.stderr)
            if done % 100 == 0:
                print(f"  … {done}/{len(work)} modules ({pages} pages)")
    print(f"rustdoc: {done} module files written, {pages} pages merged")


# ---- mdBook: one md per book --------------------------------------------------

def run_mdbook(sources: list[Path], outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    for src in sources:
        page = src / "print.html" if src.is_dir() else src
        name = src.name if src.is_dir() else src.stem
        if not page.exists():
            print(f"  ! {src}: no print.html — pass chapter files instead", file=sys.stderr)
            continue
        md = html_to_md(page.read_text(errors="ignore"), "main")
        if md is None:  # error-index.html etc. aren't mdBook; fall back to <body>
            md = html_to_md(page.read_text(errors="ignore"), "body")
        if md:
            (outdir / f"{name}.md").write_text(f"# {name}\n\n{md}")
            print(f"  ✓ {name}.md ({len(md) // 1024} KB)")
        else:
            print(f"  ! {name}: nothing extracted", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", choices=["rustdoc", "mdbook"])
    ap.add_argument("sources", nargs="+", type=Path)
    ap.add_argument("-o", "--outdir", type=Path, required=True)
    ap.add_argument("-j", "--jobs", type=int, default=20)
    args = ap.parse_args()
    if args.mode == "rustdoc":
        run_rustdoc(args.sources, args.outdir, args.jobs)
    else:
        run_mdbook(args.sources, args.outdir)


if __name__ == "__main__":
    main()
