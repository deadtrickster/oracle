// Oracle Capture — Wikipedia-style citations, shared by the popup and the in-page overlay.
//
// Deliberately a CLASSIC script assigning to globalThis, not an ES module: overlay.js is injected
// with chrome.scripting.executeScript({files:[...]}) and injected files cannot use `import`. The
// popup loads this with a plain <script> before its module, so both get ONE implementation — the
// duplication that had already caused drift between popup.js and background.js is not repeated.
(() => {
  if (globalThis.OracleCite) return;

  const esc = (s) =>
    String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  // Which [n] does the answer actually use? A retrieval pool is ~22 excerpts but an answer cites a
  // handful; listing all of them would bury the 3 that matter. Wikipedia lists only what is cited.
  // Matches [2] and the [2], [5], [7] runs the model writes, but NOT [] or [word].
  const CITE_RE = /\[(\d{1,2})\]/g;

  function used(text, citations) {
    const have = new Set((citations || []).map((c) => c.n));
    // Strip fenced and inline code first, for the same reason linkify skips <pre>/<code>: an
    // `arr[2]` in a listing must not manufacture a footnote for source 2.
    const prose = String(text).replace(/```[\s\S]*?```/g, " ").replace(/`[^`\n]*`/g, " ");
    const seen = [];
    for (const m of prose.matchAll(CITE_RE)) {
      const n = Number(m[1]);
      if (have.has(n) && !seen.includes(n)) seen.push(n);
    }
    return seen; // ORDER OF FIRST APPEARANCE — the display numbering is derived from this
  }

  /**
   * Decide the displayed numbering.
   *
   * The [n] the model writes is an EXCERPT index into the retrieval pool (1..~22). Rendering those
   * verbatim leaks an internal id into the UI and produces gaps — an answer citing excerpts 1 and 4
   * shows a "Sources" list numbered 1, 4, which reads as two missing references rather than two
   * references. Wikipedia never does this: it renumbers 1..k in order of first appearance. So map
   * excerpt -> display here, and render display everywhere. The original excerpt index is kept on
   * the anchor's title for debugging, since it is the only link back to what the model was shown.
   */
  function plan(text, citations) {
    const byN = new Map((citations || []).map((c) => [c.n, c]));
    const order = used(text, citations).map((n, i) => ({ n, display: i + 1, cite: byN.get(n) }));
    return { order, display: new Map(order.map((o) => [o.n, o.display])) };
  }

  /**
   * Turn every [n] into a superscript anchor pointing at its footnote.
   *
   * Skips <pre>/<code>. This corpus is largely programming material, so answers routinely contain
   * `arr[2]`, `argv[1]`, `positions[0]` — linkifying those would invent citations inside code and
   * corrupt a listing the reader is meant to copy. Split on code regions, transform only prose.
   */
  function linkify(html, pl, idPrefix) {
    const one = (s) =>
      s.replace(CITE_RE, (whole, d) => {
        const n = Number(d);
        const disp = pl.display.get(n);
        if (!disp) return whole; // not a resolvable citation — leave the text alone
        return `<sup class="oc-ref"><a href="#${idPrefix}-fn-${disp}" data-oc-fn="${disp}"` +
          ` title="excerpt ${n}">[${disp}]</a></sup>`;
      });
    // keep the delimiters (capturing group) so code regions pass through untouched
    return String(html)
      .split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>)/gi)
      .map((part, i) => (i % 2 ? part : one(part)))
      .join("");
  }

  /**
   * Footnote list for the cited excerpts. Each entry links into the corpus browser at the page the
   * claim came from — the point of the whole exercise: a citation you can follow, not just a name.
   */
  function footnotes(pl, idPrefix) {
    if (!pl.order.length) return "";
    const rows = pl.order.map(({ display, cite: c }) => {
      const page = c.page ? ` <span class="oc-pg">p.&nbsp;${c.page}</span>` : "";
      return (
        `<li id="${idPrefix}-fn-${display}" class="oc-fn">` +
        `<span class="oc-n">${display}.</span> ` +
        `<a href="${esc(c.url)}" target="_blank" rel="noreferrer">${esc(c.doc)}</a>${page}` +
        `</li>`
      );
    });
    return `<div class="oc-notes"><div class="oc-notes-h">Sources</div><ol class="oc-list">${rows.join("")}</ol></div>`;
  }

  // Scoped so the host page's CSS cannot restyle it, and so it reads as apparatus, not body text.
  const CSS = `
.oc-ref{font-size:.75em;line-height:0}
.oc-ref a{text-decoration:none;color:#06c;padding:0 .1em}
.oc-ref a:hover{text-decoration:underline}
.oc-notes{margin-top:.9em;border-top:1px solid rgba(128,128,128,.3);padding-top:.5em}
.oc-notes-h{font-size:.78em;text-transform:uppercase;letter-spacing:.04em;opacity:.6;margin-bottom:.3em}
.oc-list{margin:0;padding-left:1.1em;list-style:none}
.oc-fn{font-size:.86em;margin:.18em 0;opacity:.92}
.oc-fn .oc-n{opacity:.55;margin-right:.15em}
.oc-fn a{color:#06c;text-decoration:none}
.oc-fn a:hover{text-decoration:underline}
.oc-pg{opacity:.55}
.oc-fn:target{background:rgba(255,220,80,.35);border-radius:3px}
`;

  globalThis.OracleCite = { plan, linkify, footnotes, used, CSS, esc };
})();
