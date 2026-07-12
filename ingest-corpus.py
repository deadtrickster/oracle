# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""PLAN.md Step 4: create RAGFlow knowledge bases and bulk-ingest corpus/ via the HTTP API.

Prereqs (one-time, in the web UI at http://localhost):
  1. register your account
  2. Model providers -> add Ollama (base URL http://host.docker.internal:11434,
     chat model qwen3-coder:30b) and set a DEFAULT EMBEDDING model (a built-in
     CPU one, e.g. bge-m3) in tenant settings — datasets created here inherit it
  3. create an API key (avatar menu -> API)

Run:  uv run ingest-corpus.py --api-key <KEY> [--wait]
Re-runnable: existing datasets are reused, already-uploaded filenames skipped.
"""
import argparse
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).parent
C = ROOT / "corpus"

# (dataset name, chunk_method, [glob specs relative to corpus/])
KBS = [
    ("rust", "naive", [  # NOT "book": the Book parser rejects .md (doc/docx/pdf/txt only)
        "rust/book/src/**/*.md", "rust/nomicon/src/**/*.md",
        "rust/reference/src/**/*.md", "rust/rust-by-example/src/**/*.md",
        "rust/async-book/src/**/*.md",
        # OSS books harvest (2026-07-12)
        "rust/books-oss/comprehensive-rust/src/**/*.md",
        "rust/books-oss/too-many-lists/src/*.md",
        "rust/books-oss/patterns/src/**/*.md",
        "rust/books-oss/perf-book/src/*.md",
        "rust/books-oss/tlborm/src/**/*.md",
        "rust/books-oss/blog_os/blog/content/**/index.md",  # bare index.md = English only
        "rust/books-oss/embedded-rust-book/src/**/*.md",
        "rust/books-oss/rustc-dev-guide/src/**/*.md",
        "rust/books-oss/high-assurance-rust/src/**/*.md",
        "rust/books-oss/writing-interpreters-in-rust/booksrc/*.md",
    ]),
    ("rust-api", "naive", [
        "rust/api-md/**/*.md", "io_uring_rust/api-md/**/*.md",
    ]),
    ("io_uring", "naive", [
        "io_uring/man-txt/*.txt", "io_uring/*.pdf", "io_uring/io_uring.h@*",
        "io_uring/lord-of-the-io_uring/**/*.md",
    ]),
    ("linux", "naive", [
        "linux/man-merged/*.txt", "linux/kernel-docs/*",
        "linux/gnu-manuals/*",                       # bash + glibc reference manuals
        "linux/wayland-kde/wayland-book/src/**/*.md",
        "linux/wayland-kde/wayland-protocols/stable/**/*.xml",
        "linux/wayland-kde/wayland-core-protocol.xml",
        "linux/wayland-kde/archwiki/*.md",
        "linux/ubuntu/*.pdf",                        # Ubuntu Server Guide
        "linux/git-progit2/book/**/*.asc",           # Pro Git 2 (git man pages are in man-merged)
        "linux/so2/**/*.html",                        # linux-kernel-labs SO2 OS/kernel course
    ]),
    ("oracle-meta", "naive", ["meta/*"]),            # the system's own docs + scripts
    ("go", "naive", [
        "go/website/_content/doc/**/*.md", "go/website/_content/doc/**/*.html",
        "go/website/_content/ref/**/*", "go/website/_content/*.md",
        "go/go_spec.html",                           # language spec (from golang/go)
        "go/go101/pages/**/*.md",                    # Go 101 book
        "go/gobyexample/examples/**/*.go",           # annotated examples
        "go/the-little-go-book/en/*.md",
        "go/build-web-application-with-golang/en/*.md",
        # OSS books harvest (2026-07-12)
        "go/books-oss/learn-go-with-tests/*.md",
        "go/books-oss/go-blockchain/README.org", "go/books-oss/go-blockchain/doc/*.org",
        "go/books-oss/jeiwan-blockchain-blog/content/posts/*.md",
        "go/books-oss/blockchain_go/README.md", "go/books-oss/blockchain_go/*.go",
        "go/books-oss/go-internals/README.md", "go/books-oss/go-internals/chapter*/README.md",
        "go/books-oss/go-perfbook/*.md",
        "go/books-oss/high-performance-go-workshop/en/*.asciidoc",
        "go/books-oss/learninggo/*.md", "go/books-oss/learninggo/ex/**/*.md",
        "go/books-oss/learninggo/tab/*.md",
        "go/books-oss/Go-SCP/src/**/*.md",
        "go/books-oss/web-dev-golang-anti-textbook/manuscript/*.md",
        "go/stdlib/*.txt",   # Go standard library API reference (go doc -all, 2026-07-12)
    ]),
    ("cpp", "naive", ["cpp/md/**/*.md"]),              # cppreference C & C++ (sanitized HTML->md)
    ("cpp-libs", "naive", ["cpp-libs/**/*.md", "cpp-libs/**/*.rst"]),  # serenedb deps: abseil/fmt/simdjson/faiss
    ("duckdb", "naive", ["duckdb-web/docs/**/*.md"]),  # DuckDB docs (the serenedb engine)
    ("kubernetes", "naive", ["kubernetes/**/*.md"]),   # k8s docs (kubernetes/website content/en/docs)
    ("emacs", "naive", ["emacs/*.txt"]),
    ("postgres", "naive", [
        "postgres/readmes/*.txt", "postgres/*.md", "postgres/README*",
        "postgres/ru-books/*.txt",   # Russian PG books via pdftotext (DeepDoc garbles Cyrillic CID fonts)
        # OSS books harvest (2026-07-12)
        "postgres/books-oss/postgres-howtos/*.md",
        "postgres/books-oss/postgres-guide/_performance/*.md",
        "postgres/books-oss/postgres-guide/_setup/*.md",
        "postgres/books-oss/postgres-guide/_sql/*.md",
        "postgres/books-oss/postgres-guide/_tips/*.md",
        "postgres/books-oss/postgres-guide/_utilities/*.md",
        "postgres/books-oss/postgres-guide/_sexy/*.md",
    ]),
    # personal collection — populated later, skipped while empty
    ("papers", "paper", ["papers_raw/*.pdf", "papers_raw/*.PDF", "papers/**/*.md",
                         "prob-ds/papers/*.pdf"]),   # HLL, count-min, xor/fuse, minhash
    ("books", "book", ["books/*.md", "books/*.txt", "books_raw/*.pdf", "books_raw/*.PDF",
                       "books_raw/*.epub"]),
    ("links", "naive", ["links/*.md", "tooling/**/*.md"]),   # articles + C3L local-LLM watchdog
]
BATCH = 16


def api(sess, base, method, path, **kw):
    r = sess.request(method, f"{base}/api/v1{path}", timeout=300, **kw)
    r.raise_for_status()
    j = r.json()
    if j.get("code") not in (0, None):
        sys.exit(f"API error on {path}: {j}")
    return j.get("data")


SUPPORTED = {".md", ".txt", ".pdf", ".html", ".htm", ".docx", ".csv", ".json"}


def safe_name(p: Path, chunk_method: str = "naive") -> str:
    """RAGFlow keys off filename; make it unique + clean, and force a suffix the
    target parser accepts (.h/.rst/.0 -> .txt; Book/Paper parsers also reject .md)."""
    rel = p.relative_to(C)
    name = "__".join(rel.parts).replace("@", "-at-")
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED or (chunk_method in ("book", "paper") and suffix == ".md"):
        name += ".txt"
    return name


def list_docs(sess, base, dsid):  # page_size is capped at 100 server-side
    page = 1
    while True:
        data = api(sess, base, "GET",
                   f"/datasets/{dsid}/documents?page={page}&page_size=100") or {}
        docs = data.get("docs", [])
        yield from docs
        if len(docs) < 100:
            return
        page += 1


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api-key", required=True)
    ap.add_argument("--base", default="http://localhost:9380")
    ap.add_argument("--wait", action="store_true",
                    help="poll until parsing finishes (do this before flying!)")
    args = ap.parse_args()

    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {args.api_key}"

    existing = {d["name"]: d for d in
                (api(s, args.base, "GET", "/datasets?page_size=100") or [])}

    for name, chunk_method, globs in KBS:
        files = sorted({f for g in globs for f in C.glob(g) if f.is_file()})
        if not files:
            print(f"-- {name}: no files yet, skipped")
            continue
        if name in existing:
            ds = existing[name]
        else:
            # raptor/graphrag default ON and run LLM summarization + entity extraction
            # per doc through the single local model -> hours per KB. Retrieval-only here.
            ds = api(s, args.base, "POST", "/datasets",
                     json={"name": name, "chunk_method": chunk_method,
                           "parser_config": {"raptor": {"use_raptor": False},
                                             "graphrag": {"use_graphrag": False}}})
        dsid = ds["id"]

        # re-queue alongside new uploads: never-parsed docs (UNSTART), failed ones,
        # and "done" docs with zero chunks (parsed while no embedding model was set).
        # NOT docs currently RUNNING — re-queuing those duplicates their tasks.
        have, new_ids = set(), []
        for d in list_docs(s, args.base, dsid):
            have.add(d["name"])
            run = d.get("run")
            if run in ("UNSTART", "FAIL") or (run == "DONE" and not d.get("chunk_count")):
                new_ids.append(d["id"])
        todo = [f for f in files if safe_name(f, chunk_method) not in have]
        print(f"== {name}: {len(files)} files ({len(todo)} new, {len(have)} already up,"
              f" {len(new_ids)} unparsed to re-queue)")

        for i in range(0, len(todo), BATCH):
            chunk = todo[i:i + BATCH]
            multipart = [("file", (safe_name(f, chunk_method), f.open("rb"))) for f in chunk]
            try:
                data = api(s, args.base, "POST",
                           f"/datasets/{dsid}/documents", files=multipart)
                new_ids += [d["id"] for d in (data or [])]
            finally:
                for _, (_, fh) in multipart:
                    fh.close()
            print(f"   uploaded {min(i + BATCH, len(todo))}/{len(todo)}")
        if new_ids:
            api(s, args.base, "POST", f"/datasets/{dsid}/chunks",
                json={"document_ids": new_ids})
            print(f"   parsing started for {len(new_ids)} docs")

    if args.wait:
        print("\nWaiting for parsing to finish (Ctrl-C is safe; parsing continues server-side)")
        while True:
            busy = failed = 0
            for name, _, _ in KBS:
                if name not in existing:
                    ds = {d["name"]: d for d in
                          (api(s, args.base, "GET", "/datasets?page_size=100") or [])}
                    existing.update(ds)
                    if name not in existing:
                        continue
                dsid = existing[name]["id"]
                for d in list_docs(s, args.base, dsid):
                    st = d.get("run", "")
                    if st in ("RUNNING", "UNSTART", "0", "1"):
                        busy += 1
                    elif st in ("FAIL", "4"):
                        failed += 1
            print(f"   pending: {busy}, failed: {failed}")
            if busy == 0:
                break
            time.sleep(30)
        print("DONE — spot-check parsed chunks in the UI, then run the offline drill.")


if __name__ == "__main__":
    main()
