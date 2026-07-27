// Oracle Capture — the one SSE reader.
//
// This lived twice (background.js and popup.js) in near-identical copies, and they had already
// drifted: an error-handling bug fixed in one was invisible in the other. One protocol, one
// implementation — the same rule the rest of the project follows.
//
// It stays hand-written on purpose. Server-sent events are a two-field line protocol — split on a
// blank line, read `event:` and `data:` — and this implementation is verified byte-exact against a
// live 13 KB stream re-split at 64/7/1-byte boundaries (identical reconstruction every time). The
// off-the-shelf alternatives cost a bundler and 100 KB+ to replace ~25 lines, and would still not
// remove the custom branch for our `sources` frames, which have no place in an OpenAI delta.

/** Parse ONE SSE event block (the text between blank lines) -> {event, data} | null. */
export function parseSSE(chunk) {
  let event = "message", data = "";
  for (const line of chunk.split("\n")) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    // Multiple data: lines concatenate. Our receiver emits single-line JSON (json.dumps never
    // emits a raw newline — they arrive escaped), so this is exact for our wire.
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  try { return { event, data: JSON.parse(data) }; } catch (_) { return null; }
}

/**
 * Read a ReadableStream of SSE bytes, calling `send({event, data})` per event.
 * Events are split on the blank-line delimiter and buffered across reads, because an event can be
 * cut in half by an arbitrary TCP chunk boundary.
 */
export async function pumpSSE(stream, send) {
  const reader = stream.getReader();
  const dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const ev = parseSSE(buf.slice(0, i));
      buf = buf.slice(i + 2);
      if (ev) send(ev);
    }
  }
}
