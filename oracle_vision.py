"""The text model's eyes: qwen3-vl reads an image, and its reading is cached forever.

Used by the Claude-Code shim (a chat that references an image) and available to anything else that
needs pixels turned into text. Two design points worth stating, because both are load-bearing:

1. THE CACHE IS NOT AN OPTIMISATION. Claude Code re-sends the ENTIRE transcript on every turn, so
   an image pasted once is present in every subsequent request. Without a content-addressed cache
   each turn would swap the GPU twice (text -> vl -> text, minutes each way) to re-read a picture
   nothing about which has changed. Keyed by sha256 of the bytes, so the same image pasted in a
   different session, under a different filename, is still one read.

2. VL REPORTS, IT DOES NOT ANSWER. The vision model transcribes and describes; the text model
   reasons. Letting the weaker model answer and passing its conclusion off as "what the image
   says" would smuggle an unverified claim into the context as an observation — the exact
   confident-wrong-answer shape this repo exists to prevent. So the prompt asks for observations
   and explicitly withholds the question's answer, and the injected block is labelled as one
   model's reading rather than as the image itself.
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

VL_URL = os.environ.get("ORACLE_VL_URL", "http://localhost:18081").rstrip("/")
VL_MODEL = os.environ.get("ORACLE_VL_MODEL", "qwen3-vl")
VL_TIMEOUT = int(os.environ.get("ORACLE_VL_TIMEOUT", "420"))
VL_MAX_TOKENS = int(os.environ.get("ORACLE_VL_MAX_TOKENS", "1200"))

CACHE_PATH = Path(os.environ.get(
    "ORACLE_VISION_CACHE",
    str(Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "oracle" /
        "vision-cache.json")))
CACHE_MAX = int(os.environ.get("ORACLE_VISION_CACHE_MAX", "500"))

SYSTEM = (
    "You are the eyes of another model that cannot see. It will answer the user; you only report "
    "what is in the image, so that its answer rests on observation instead of guesswork.\n"
    "Rules:\n"
    "- Transcribe visible text VERBATIM: titles, labels, legends, axis ticks and units, table "
    "cells, error messages, code, version strings, timestamps.\n"
    "- Describe structure and what is actually plotted or shown: how many panels, what kind of "
    "chart, the shape of each series, where the notable features are.\n"
    "- Give numbers when they are readable, and say \"illegible\" when they are not. Never round a "
    "number you cannot read into one you can.\n"
    "- Report only what is present. Do not infer the system, the cause, or what it means.\n"
    "- Do NOT answer the user's question. The other model does that. Your job is to make its "
    "answer possible."
)


def sha(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def _load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text())
    except Exception:
        return {}


def cached(key: str) -> str | None:
    e = _load().get(key)
    return e.get("text") if isinstance(e, dict) else None


def remember(key: str, text: str, question: str = "", label: str = "") -> None:
    """Write-through, keeping the newest CACHE_MAX entries. Best-effort: a cache that cannot be
    written must not break the request it was meant to make cheaper."""
    try:
        c = _load()
        c[key] = {"text": text, "question": question[:300], "label": label[:200],
                  "model": VL_MODEL, "at": time.time()}
        if len(c) > CACHE_MAX:
            for k, _ in sorted(c.items(), key=lambda kv: kv[1].get("at", 0))[:len(c) - CACHE_MAX]:
                c.pop(k, None)
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(c))
        os.replace(tmp, CACHE_PATH)      # atomic: two processes must never read a half-written file
    except Exception:
        pass


def describe(image_data_url: str, question: str = "", label: str = "") -> str:
    """Ask qwen3-vl to read the image. Assumes it is ALREADY resident — swapping is the caller's
    job (oracle_vram.ensure), because only the caller knows how to show progress for something
    that takes minutes."""
    ask = ["Read this image."]
    if label:
        ask.append(f"It was provided as: {label}")
    if question:
        # The question focuses WHAT TO TRANSCRIBE — a question about latency should get the latency
        # panel read carefully — without licensing the vision model to answer it.
        ask.append("The user asked the other model: \"" + question.strip()[:500] + "\"\n"
                   "Make sure anything in the image bearing on that is transcribed precisely. "
                   "Do not answer it yourself.")
    req = {
        "model": VL_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": image_data_url}},
                {"type": "text", "text": "\n\n".join(ask)}]}],
        "max_tokens": VL_MAX_TOKENS,
        "temperature": 0.2,
        "stream": False,
    }
    r = urllib.request.Request(f"{VL_URL}/v1/chat/completions",
                               data=json.dumps(req).encode(),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=VL_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode())
    choices = payload.get("choices") or []
    text = ((choices[0].get("message") or {}).get("content") or "").strip() if choices else ""
    if not text:
        raise RuntimeError(f"qwen3-vl returned no content: {json.dumps(payload)[:300]}")
    return text


def block(text: str, label: str = "") -> str:
    """The description as it goes INTO the text model's context.

    Labelled, always. The text model must not mistake another model's reading for the image
    itself: it is evidence with a known author and a known failure mode, and downstream answers
    should be able to say "the vision model read X" rather than asserting X."""
    what = f" ({label})" if label else ""
    return (f"[IMAGE{what} — read by qwen3-vl, the local vision model. You cannot see the picture; "
            f"the text below is that model's reading of it. Treat it as an observation report, not "
            f"as ground truth, and say so if you rely on a detail that could have been misread.]\n"
            f"{text}\n[end of image reading]")
