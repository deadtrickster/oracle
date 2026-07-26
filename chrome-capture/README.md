# Oracle Capture — browse-time corpus ingest + "explain from the corpus"

A Chrome (MV3) extension that feeds Oracle from the one place server-side `fetch_url` can't reach:
**your logged-in, already-rendered browser tab.** Two features, one tiny local receiver
(`../oracle-capture-receiver.py`):

1. **Capture page → corpus.** One click (or `Ctrl+Shift+Y`, or right-click → *Capture page to
   Oracle*) turns the live DOM into clean Markdown — via the **same trafilatura** `fetch_url` uses,
   so captures match the rest of the corpus — plus an archived **PDF** of the rendered page. It
   lands in `corpus/inbox/captures/` and is ingested into the **`links`** knowledge base.

2. **Explain this with Oracle.** Select text → right-click → *Explain this with Oracle*. A popup
   "glued" to your selection answers **from your corpus only**, using the exact `ask_corpus`
   pipeline (bge-m3 retrieval → gte reranker → qwen synthesis, "answer from the excerpts or say the
   corpus doesn't cover it"). Trustworthy offline, with sources.

## Why a browser extension (not just `fetch_url`)

`fetch_url` runs on the backend, so it never sees pages behind your login, a paywall, or heavy JS —
and it can't render a faithful PDF. The extension captures the **authenticated, rendered** DOM and a
real `Page.printToPDF`, then hands both to the receiver, which reuses Oracle's existing ingest and
ask pipelines. Markdown is the retrieval source (the PDF→text path is the lossy one this repo keeps
fighting); the PDF is kept for visual reference.

## Two-layer offline buffering (works mid-flight)

- **Extension → receiver unreachable** (laptop app not running): the capture is buffered in
  `chrome.storage` and retried every minute; the toolbar badge shows the backlog.
- **Receiver → RAGFlow down** (backend off in the air): the receiver still writes the `.md`+`.pdf`
  immediately and records a `pending` job; a background drainer ingests it whenever RAGFlow returns.

So you can capture with the whole stack off and it all lands later. Explain, of course, needs the
backend up (it queries the models).

## Install

**1. Run the receiver** on the Oracle backend (the laptop):

```bash
python3 ~/Projects/oracle/oracle-capture-receiver.py       # binds 127.0.0.1:8788 only
```

It reads the same env vars as the ask/ingest MCP tools (`ORACLE_RAGFLOW_URL`, `ORACLE_RAGFLOW_KEY`,
`ORACLE_OLLAMA_URL`, `ORACLE_SYNTH_MODEL`, `ORACLE_CORPUS`; plus `ORACLE_CAPTURE_PORT`=8788,
`ORACLE_CAPTURE_DATASET`=links). As a systemd user unit matching the rest of the stack
(`oracle-capture` is already in `oracle-ctl.sh`'s service list):

```ini
# ~/.config/systemd/user/oracle-capture.service
[Unit]
Description=Oracle capture receiver (Chrome extension endpoint)
After=network.target
[Service]
ExecStart=%h/Projects/oracle/oracle-capture-receiver.py
Restart=on-failure
[Install]
WantedBy=default.target
```
```bash
systemctl --user enable --now oracle-capture
```

**2. Load the extension:** `chrome://extensions` → enable *Developer mode* → *Load unpacked* →
select this `chrome-capture/` folder.

## Use

- **Capture:** toolbar button → *Capture this page*, or `Ctrl+Shift+Y`, or right-click the page.
  The popup shows receiver / RAGFlow / synth health and the queued-capture count, and lets you
  toggle PDF capture (PDF uses the DevTools protocol, which briefly shows Chrome's "started
  debugging this browser" banner — turn it off if that bugs you).
- **Explain:** select text → right-click → *Explain this with Oracle*. Drag the card by its title
  bar; `×` to close.

## Notes / caveats

- **Local only.** The receiver binds `127.0.0.1` — never the LAN. All extension↔receiver traffic
  goes through the **background service worker** (extension origin), because an `https://` page is
  blocked from fetching `http://localhost` (mixed content).
- **No bundled icons** — Chrome uses a default action icon; drop `icon16/48/128.png` here and add an
  `"icons"` block to `manifest.json` if you want branding.
- **Dedup:** RAGFlow rejects a duplicate filename; re-capturing the same page (same second) is a
  no-op on the backend. Captures are timestamped, so re-reading a page later makes a new doc.
- `corpus/` is gitignored, so captured content never gets committed.
