import os
import json
import uuid
import urllib.request
import re

import boto3

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")

AUDIO_BUCKET = os.environ["AUDIO_BUCKET"]
TRANSCRIPT_BUCKET = os.environ.get("TRANSCRIPT_BUCKET", AUDIO_BUCKET)
LANGUAGE_CODE = os.environ.get("LANGUAGE_CODE", "en-US")
MAX_SPEAKERS = int(os.environ.get("MAX_SPEAKERS", "10"))


def _response(status_code: int, body: dict):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Allow-Methods": "OPTIONS,POST",
        },
        "body": json.dumps(body),
    }


def _get_path(event) -> str:
    return event.get("rawPath") or event.get("path") or ""


def _read_json_body(event) -> dict:
    body_raw = event.get("body") or ""
    if not body_raw:
        return {}
    if event.get("isBase64Encoded"):
        import base64
        body_raw = base64.b64decode(body_raw).decode("utf-8", errors="replace")
    return json.loads(body_raw)


# -------------------------
# De-duplication utilities
# -------------------------

def _norm_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _dedupe_immediate_repeated_ngrams(text: str, n_min: int = 2, n_max: int = 12) -> str:
    """
    Removes immediate repeated n-grams (exact repeated word sequences).
    n_min=2 ensures we do NOT remove single-word repeats like "yes yes".
    This fixes cases like:
      "At 11:30 At 11:30"
      "Sure with cookies Sure with cookies"
      "That's good, thank you. That's good, thank you."
    """
    words = _norm_space(text).split()
    if len(words) < 2 * n_min:
        return " ".join(words)

    i = 0
    out = []
    while i < len(words):
        removed = False
        max_n_here = min(n_max, (len(words) - i) // 2)
        for n in range(max_n_here, n_min - 1, -1):
            a = words[i:i + n]
            b = words[i + n:i + 2 * n]
            if a == b:
                out.extend(a)        # keep one copy
                i += 2 * n
                removed = True
                break
        if not removed:
            out.append(words[i])
            i += 1

    return " ".join(out)


def _dedupe_text(text: str) -> str:
    # Run twice to catch cascaded repeats (rare but happens)
    t = _dedupe_immediate_repeated_ngrams(text, n_min=2, n_max=12)
    t = _dedupe_immediate_repeated_ngrams(t, n_min=2, n_max=12)
    return _norm_space(t)


def _drop_consecutive_duplicate_lines(lines):
    """
    Drops consecutive duplicate lines even if the speaker differs.
    Fixes cases like:
      speaker 1: "How many words do you need"
      speaker 3: "How many words do you need"
    """
    cleaned = []
    prev_key = None
    for ln in lines:
        txt = _norm_space(ln.get("text", "")).lower()
        if not txt:
            continue
        if txt == prev_key:
            continue
        cleaned.append(ln)
        prev_key = txt
    return cleaned


# -------------------------
# Speaker turns extraction
# -------------------------

def _speaker_turns_from_transcribe_json(transcript_json: dict):
    results = transcript_json.get("results", {})
    items = results.get("items", [])
    speaker_segments = (results.get("speaker_labels", {}) or {}).get("segments", [])

    # Fallback if diarization missing
    if not speaker_segments:
        full_text = (results.get("transcripts", [{}])[0] or {}).get("transcript", "")
        full_text = _dedupe_text(full_text)
        return 1, [{"speaker": "speaker 1", "text": full_text}]

    # Segment timeline (sorted)
    segments = sorted(
        [
            {
                "speaker": seg["speaker_label"],
                "start": float(seg["start_time"]),
                "end": float(seg["end_time"]),
            }
            for seg in speaker_segments
            if "start_time" in seg and "end_time" in seg and "speaker_label" in seg
        ],
        key=lambda x: x["start"],
    )

    speaker_lines = []
    current_speaker = None
    current_text = ""
    seg_index = 0

    def flush():
        nonlocal current_text, current_speaker
        if current_speaker is not None:
            txt = _norm_space(current_text)
            if txt:
                speaker_lines.append({"speaker": current_speaker, "text": txt})
        current_text = ""

    for item in items:
        typ = item.get("type")

        if typ == "pronunciation":
            start_time = float(item["start_time"])

            # Move to the correct segment
            while seg_index < len(segments) and start_time > segments[seg_index]["end"]:
                seg_index += 1
            if seg_index >= len(segments):
                break

            speaker = segments[seg_index]["speaker"]
            word = item["alternatives"][0]["content"]

            # Speaker change -> new line
            if speaker != current_speaker:
                flush()
                current_speaker = speaker

            # Add word with spacing
            if not current_text:
                current_text = word
            else:
                current_text += " " + word

        elif typ == "punctuation":
            # Attach punctuation to the current text (no extra space)
            if current_text:
                punct = item["alternatives"][0]["content"]
                current_text += punct

    flush()

    # Normalize speaker labels to speaker 1..N using stable order spk_0, spk_1...
    def spk_key(s):
        try:
            return int(str(s).split("_")[1])
        except Exception:
            return 999999

    unique = sorted({ln["speaker"] for ln in speaker_lines if ln.get("speaker")}, key=spk_key)
    mapping = {sp: f"speaker {i+1}" for i, sp in enumerate(unique)}

    for ln in speaker_lines:
        ln["speaker"] = mapping.get(ln["speaker"], "speaker 1")
        ln["text"] = _dedupe_text(ln.get("text", ""))

    # Drop consecutive duplicates (even across speakers)
    speaker_lines = _drop_consecutive_duplicate_lines(speaker_lines)

    return (len(unique) if unique else 1), speaker_lines


# -------------------------
# Handlers
# -------------------------

def _handle_presign(event):
    payload = _read_json_body(event)
    filename = payload.get("filena
