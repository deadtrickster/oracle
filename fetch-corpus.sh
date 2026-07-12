#!/usr/bin/env bash
# Fetch the offline doc corpus for the Oracle assistant. Run ONLINE, before travel.
# Lands everything under ./corpus/ as clean text/markdown, ready for RAGFlow ingestion.
set -uo pipefail
cd "$(dirname "$0")"
mkdir -p corpus/{rust,io_uring/man-txt,io_uring_rust/api,linux/man,linux/kernel-docs,emacs,books}
clone(){ [ -d "$2/.git" ] && (cd "$2" && git pull -q) || git clone --depth 1 -q "$1" "$2"; echo "  ✓ $2"; }
ROOT=$(pwd)

echo "== Rust prose (markdown sources) =="
clone https://github.com/rust-lang/nomicon          corpus/rust/nomicon
clone https://github.com/rust-lang/book              corpus/rust/book
clone https://github.com/rust-lang/reference         corpus/rust/reference
clone https://github.com/rust-lang/rust-by-example   corpus/rust/rust-by-example
clone https://github.com/rust-lang/async-book        corpus/rust/async-book      # NOT in rustup bundle
echo "== Rust API + tooling books (already offline HTML, 785M — link, don't copy) =="
SR=$(rustc --print sysroot)
ln -sfn "$SR/share/doc/rust/html" corpus/rust/rustup-html
echo "  ✓ linked rustup-html → std/core/alloc/proc_macro API + book/reference/nomicon/by-example +"
echo "     cargo/clippy/rustc/rustdoc/edition-guide/embedded-book/unstable-book + error index"
echo "  std SOURCE (rust-src): $SR/lib/rustlib/src/rust/library   (ingest for deep std internals)"
echo "  ▶ MAX crate docs: in the waldump project run 'cargo doc' → target/doc (whole dep tree), ingest that"

echo "== io_uring =="
clone https://github.com/axboe/liburing              corpus/io_uring/liburing
clone https://github.com/shuveb/loti                 corpus/io_uring/lord-of-the-io_uring
curl -fsSL -o corpus/io_uring/efficient-io-with-io_uring.pdf https://kernel.dk/io_uring.pdf \
  && echo "  ✓ Axboe paper" || echo "  ! grab kernel.dk/io_uring.pdf manually"
# GROUND TRUTH for the TARGET kernel: which ops/flags actually exist. Pin to 7.0 (your bootable 7.x).
KTAG=v7.0
curl -fsSL --retry 4 "https://raw.githubusercontent.com/torvalds/linux/$KTAG/include/uapi/linux/io_uring.h" \
  -o "corpus/io_uring/io_uring.h@$KTAG" && echo "  ✓ io_uring.h @$KTAG (the op/flag allowlist for kernel 7.0)"
CONV="man --"; command -v mandoc >/dev/null && CONV="mandoc -Tutf8"
n=0; for f in corpus/io_uring/liburing/man/*.[237]; do [ -e "$f" ] || continue
  $CONV "$f" 2>/dev/null | col -bx > "corpus/io_uring/man-txt/$(basename "$f").txt" && n=$((n+1)); done
echo "  ✓ $n io_uring man pages"

echo "== io_uring Rust crates (source + offline rustdoc) =="
clone https://github.com/tokio-rs/io-uring           corpus/io_uring_rust/io-uring
clone https://github.com/tokio-rs/tokio-uring        corpus/io_uring_rust/tokio-uring
# full 'cargo doc' (with deps): --no-deps would document only the empty stub crate,
# and '-p io-uring' is ambiguous (0.7 direct + 0.6 via tokio-uring)
tmp=$(mktemp -d); ( cd "$tmp" && cargo init -q --name uringdocs \
  && cargo add -q io-uring tokio-uring 2>/dev/null && cargo doc -q 2>/dev/null )
[ -d "$tmp/target/doc" ] && cp -r "$tmp/target/doc/." corpus/io_uring_rust/api/ && echo "  ✓ crate rustdoc"
rm -rf "$tmp"

echo "== Linux / devops knowledge (the big sysadmin haul) =="
# Local man pages → text. Sections: 1=cmds 2=syscalls 3=libc/API 5=configs 7=overviews 8=admin.
# Captures cgroups(7), proc(5)/proc_sys_vm(5), sysctl(8), namespaces(7), systemd.*(5) incl.
# systemd.resource-control(5) [MemoryMax/cgroup RSS], + all of section 3 (C library, pthreads, etc).
m=0; for s in 1 2 3 5 7 8; do
  for f in /usr/share/man/man$s/*.$s.gz /usr/share/man/man$s/*.$s; do [ -e "$f" ] || continue
    b=$(basename "$f"); b=${b%.gz}
    man "$f" 2>/dev/null | col -bx > "corpus/linux/man/$b.txt" 2>/dev/null && m=$((m+1)); done; done
echo "  ✓ $m system man pages → corpus/linux/man/"
# Kernel admin docs pinned to the TARGET kernel (7.0): dirty-page sysctls, cgroup v2 RSS limits, etc.
KB=https://raw.githubusercontent.com/torvalds/linux/v7.0/Documentation/admin-guide
# (laptops/laptop-mode.rst was removed upstream in v7.0 — legacy; dirty-page knobs live in vm.rst)
for f in sysctl/vm.rst sysctl/kernel.rst sysctl/fs.rst sysctl/net.rst cgroup-v2.rst \
         mm/concepts.rst pm/index.rst; do
  o="corpus/linux/kernel-docs/$(echo "$f" | tr '/' '_')"
  curl -fsSL --retry 4 "$KB/$f" -o "$o" 2>/dev/null && echo "  ✓ $(basename "$f")" || echo "  ! retry $(basename "$f") (rate-limited)"; done
echo "  (optional extra: Arch Wiki — add a markdown mirror clone into corpus/linux/archwiki/ if you want it)"

echo "== GNU manuals (bash + glibc) =="
mkdir -p corpus/linux/gnu-manuals
curl -fsSL -o corpus/linux/gnu-manuals/bash-manual.txt https://www.gnu.org/software/bash/manual/bash.txt && echo "  ✓ bash manual"
curl -fsSL -o /tmp/libc-mono.$$.html https://sourceware.org/glibc/manual/latest/html_mono/libc.html \
  && pandoc /tmp/libc-mono.$$.html -f html -t gfm --wrap=none -o corpus/linux/gnu-manuals/glibc-manual.md \
  && rm -f /tmp/libc-mono.$$.html && echo "  ✓ glibc manual (7 MB html → md)"

echo "== Wayland / KDE =="
mkdir -p corpus/linux/wayland-kde
clone https://git.sr.ht/~sircmpwn/wayland-book                       corpus/linux/wayland-kde/wayland-book
clone https://gitlab.freedesktop.org/wayland/wayland-protocols.git   corpus/linux/wayland-kde/wayland-protocols
curl -fsSL -o corpus/linux/wayland-kde/wayland-core-protocol.xml \
  https://gitlab.freedesktop.org/wayland/wayland/-/raw/main/protocol/wayland.xml && echo "  ✓ core protocol xml"
if command -v trafilatura >/dev/null; then
  mkdir -p corpus/linux/wayland-kde/archwiki
  for u in Wayland KDE SDDM HiDPI Qt Xorg; do echo "https://wiki.archlinux.org/title/$u"; done > /tmp/wk-urls.$$
  echo "https://community.kde.org/KWin/Wayland" >> /tmp/wk-urls.$$
  trafilatura --input-file /tmp/wk-urls.$$ --output-dir corpus/linux/wayland-kde/archwiki --markdown 2>/dev/null
  rm -f /tmp/wk-urls.$$; echo "  ✓ $(ls corpus/linux/wayland-kde/archwiki | wc -l) wiki pages"
fi

echo "== linux-kernel-labs SO2 course (HTML + diagram images) =="
SO2=corpus/linux/so2
mkdir -p "$SO2/_images"
wget -q -r -np -nH --cut-dirs=3 -A '*.html,*.css' -e robots=off \
  "https://linux-kernel-labs.github.io/refs/heads/master/so2/" 2>/dev/null
# GH Pages has no _images/ index, so fetch each referenced diagram directly (must-have for reading)
IMGBASE="https://linux-kernel-labs.github.io/refs/heads/master/_images"
grep -ohP '\.\./_images/\K[^"]+\.(png|svg|jpg|jpeg|gif)' "$SO2"/so2/*.html 2>/dev/null | sort -u | while read -r img; do
  curl -fsSL -o "$SO2/_images/$img" "$IMGBASE/$img" 2>/dev/null
done
echo "  ✓ $(find "$SO2" -name '*.html' | wc -l) SO2 pages, $(find "$SO2/_images" -type f | wc -l) images"

echo "== Ubuntu Server Guide =="
mkdir -p corpus/linux/ubuntu
curl -fsSL -o corpus/linux/ubuntu/ubuntu-server-guide.pdf \
  "https://documentation.ubuntu.com/server/_/downloads/en/latest/pdf/" && echo "  ✓ ubuntu server guide pdf"

echo "== Oracle meta (teach the assistant about itself) =="
mkdir -p corpus/meta
cp OPERATIONS.md PLAN.md ingest-corpus.py sanitize-apidocs.py fetch-corpus.sh \
   prep-collection.sh setup-ollama.sh pull-models.sh corpus/meta/ 2>/dev/null && echo "  ✓ meta docs + scripts"

echo "== Emacs manuals (self-built texinfo → plaintext) =="
ED=$HOME/bin/emacs/doc
if command -v makeinfo >/dev/null 2>&1 && [ -d "$ED" ]; then
  # -I "$ED/emacs": docstyle.texi + emacsver.texi live there; without it lispref/lispintro/misc fail
  MI="makeinfo -I $ED/emacs -I . --no-split --plaintext"
  ( cd "$ED/emacs"     && $MI -o "$ROOT/corpus/emacs/emacs-manual.txt"    emacs.texi ) 2>/dev/null && echo "  ✓ Emacs manual"
  ( cd "$ED/lispref"   && $MI -o "$ROOT/corpus/emacs/elisp-reference.txt" elisp.texi ) 2>/dev/null && echo "  ✓ Elisp reference"
  ( cd "$ED/lispintro" && $MI -o "$ROOT/corpus/emacs/elisp-intro.txt"     emacs-lisp-intro.texi ) 2>/dev/null && echo "  ✓ Elisp intro"
  k=0; for t in "$ED"/misc/*.texi; do [ -e "$t" ] || continue
    case "$(basename "$t")" in doclicense*|gpl*|trampver*|docstyle*|gnus-faq*|sem-user*) continue;; esac
    ( cd "$ED/misc" && $MI -o "$ROOT/corpus/emacs/misc-$(basename "${t%.texi}").txt" "$(basename "$t")" ) 2>/dev/null && k=$((k+1)); done
  echo "  ✓ $k misc manuals (org, calc, cl, tramp, …)"
else
  for i in "$HOME"/bin/emacs/info/{emacs,elisp,eintr}.info; do [ -e "$i" ] && cp "$i" "corpus/emacs/$(basename "$i").txt"; done
  echo "  ✓ copied built .info files (makeinfo unavailable)"
fi

echo "== PostgreSQL (Oriole targets PG 17.9: manual + internals + source READMEs) =="
mkdir -p corpus/postgres/readmes
PGSRC=$HOME/Projects/orioledb/orioledb-postgres
if [ -d "$PGSRC" ]; then
  find "$PGSRC/src" -iname 'README*' 2>/dev/null | while read -r r; do
    cp "$r" "corpus/postgres/readmes/$(echo "${r#$PGSRC/src/}" | tr '/' '_').txt"; done
  echo "  ✓ $(ls corpus/postgres/readmes 2>/dev/null | wc -l) PG17 source READMEs (WAL=access_transam, page layout, buffers, MVCC)"
fi
echo "  ▶ books/manuals: add by hand — see corpus/postgres/BOOKS-TO-ADD.md"
echo "     (official PG17 manual · Rogov 'PG Internals' · Suzuki 'The Internals of PostgreSQL' [HTML] · Postgres Pro others)"
OB=$HOME/Projects/orioledb/orioledb
[ -d "$OB" ] && { cp "$OB"/README* corpus/postgres/ 2>/dev/null; find "$OB/doc" -name '*.md' 2>/dev/null -exec cp {} corpus/postgres/ \; ; echo "  ✓ OrioleDB own README/docs (storage + undo design)"; }

echo; echo "DONE. Corpus sizes:"; du -sh corpus/* 2>/dev/null | sed 's/^/  /'
echo "Next: point RAGFlow at corpus/ (Step 4). Personal files (bookmarks/papers/books): Step 3b."
