#!/usr/bin/env bash
# Render mdBook sources -> proper HTML sites (native sticky sidebar, search) under corpus/_rendered
set -uo pipefail
cd "$(dirname "$0")/corpus"
MDBOOK="$HOME/.local/bin/mdbook"
REND="$PWD/_rendered"; mkdir -p "$REND"
BOOKS=(
  rust/book rust/nomicon rust/reference rust/rust-by-example rust/async-book
  rust/books-oss/embedded-rust-book rust/books-oss/tlborm rust/books-oss/high-assurance-rust
  rust/books-oss/patterns rust/books-oss/rustc-dev-guide rust/books-oss/writing-interpreters-in-rust
  rust/books-oss/too-many-lists rust/books-oss/comprehensive-rust rust/books-oss/perf-book
  linux/wayland-kde/wayland-book
)
: > "$REND/_index_items.html"
for b in "${BOOKS[@]}"; do
  [ -f "$b/book.toml" ] || continue
  name=$(echo "$b" | tr '/' '-')
  if "$MDBOOK" build "$b" -d "$REND/$name" >/dev/null 2>&1; then
    echo "  ✓ $name"
    echo "<li><a href=\"$name/index.html\">$name</a></li>" >> "$REND/_index_items.html"
  else echo "  ! $name failed"; fi
done
{ echo "<!doctype html><meta charset=utf-8><title>Oracle rendered books</title>"
  echo "<style>body{font:16px/1.6 system-ui;max-width:50em;margin:3em auto;padding:0 1em}h1{border-bottom:1px solid #ccc}</style>"
  echo "<h1>Rendered books</h1><p>mdBook-rendered with sidebar + search. Raw corpus: <a href=\"/\">/</a></p><ul>"
  cat "$REND/_index_items.html"; echo "</ul>"; } > "$REND/index.html"
echo "done -> $REND/index.html"
