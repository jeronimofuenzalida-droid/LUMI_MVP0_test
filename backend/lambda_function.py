import os
import json
import time
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
# De-duplication helpers
# -------------------------

def _normalize_space(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def _dedupe_exact_sentence_repeat(text: str) -> str:
    """
    Removes exact back-to-back repetition like:
      "A. A." or "A? A?" or "A A"
    """
    t = _normalize_space(text)
    if not t:
        return t

    # Whole-string exact double
    if len(t) % 2 == 0:
        mid = len(t) // 2
        left, right = t[:mid].strip(), t[mid:].strip()
        if left and left == right:
            return left

    # Sentence-level immediate duplicates
    parts = re.split(r"(?<=[.!?])\s+", t)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and p == out[-1]:
            continue
        out.append(p)
    return " ".join(out)


def _dedupe_repeated_ngrams(text: str, n_min: int = 5, n_max: int = 12) -> str:
    """
    Removes immediate repeated n-grams (word sequences), conservatively.
    Example:
      "my throat hurts my throat hurts" -> "my throat hurts"
    """
    words = _normalize_space(text).split()
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
                out.extend(a)          # keep one copy
                i += 2 * n
                removed = True
                break
        if not removed:
            out.append(words[i])
            i += 1

    return " ".join(out)


def _dedupe_text(text: str) -> str:
    t = _dedupe_exact_sentence_repeat(text)
    t = _dedupe_repeated_ngrams(t)
    return t


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

    # Build segment timeline with start/end times
    segments = []
    for seg in speaker_segments:
        segments.append({
            "speaker": seg["speaker_label"],
            "start": float(seg["start_time"]),
            "end": float(seg["end_time"]),
        })

    speaker_lines = []
    current_speaker = None
    current_tokens = []
    seg_index = 0

    for item in items:
        if item.get("type") != "pronunciation":
            continue

        start_time = float(item["start_time"])

        # Advance segment pointer
        while seg_index < len(segments) and start_time > segments[seg_index]["end"]:
            seg_index += 1

        if seg_index >= len(segments):
            break

        speaker = segments[seg_index]["speaker"]

        if speaker != current_speaker:
            if current_tokens:
                speaker_lines.append({
                    "speaker": current_speaker,
                    "text": " ".join(current_tokens),
                })
            current_speaker = speaker
            current_tokens = []

        current_tokens.append(item["alternatives"][0]["content"])

    if current_tokens:
        speaker_lines.append({
            "speaker": current_speaker,
            "text": " ".join(current_tokens),
        })

    # Normalize speaker labels (stable numbering)
    unique = sorted(set(line["speaker"] for line in speaker_lines if line["speaker"]))
    mapping = {sp: f"speaker {i + 1}" for i, sp in enumerate(unique)}

    for line in speaker_lines:
        line["speaker"] = mapping.get(line["speaker"], "speaker 1")
        line["text"] = _dedupe_text(line.get("text", ""))

    # Drop empty lines after dedupe
    speaker_lines = [l for l in speaker_lines if _normalize_space(l.get("text", ""))]

    return (len(unique) if unique else 1), speaker_lines


# -------------------------
# Handlers
# -------------------------

def _handle_presign(event):
    payload = _read_json_body(event)
    filename = payload.get("filename", "audio")
    content_type = payload.get("content_type", "application/octet-stream")

    ext = (filename.split(".")[-1] or "").lower()
    if ext not in {"mp3", "wav", "m4a"}:
        return _response(400, {"error": f"Unsupported file type: .{ext}. Use mp3/wav/m4a."})

    key = f"uploads/{uuid.uuid4().hex}.{ext}"

    url = s3.generate_presigned_url(
        ClientMethod="put_object",
        Params={"Bucket": AUDIO_BUCKET, "Key": key, "ContentType": content_type},
        ExpiresIn=600,
    )
    return _response(200, {"upload_url": url, "s3_key": key})


def _handle_transcribe_start(event):
    payload = _read_json_body(event)
    s3_key = payload.get("s3_key")
    if not s3_key:
        return _response(400, {"error": "Missing 's3_key'."})

    media_uri = f"s3://{AUDIO_BUCKET}/{s3_key}"
    job_name = f"lumi-{uuid.uuid4().hex}"

    transcribe.start_transcription_job(
        TranscriptionJobName=job_name,
        LanguageCode=LANGUAGE_CODE,
        Media={"MediaFileUri": media_uri},
        Settings={
            "ShowSpeakerLabels": True,
            "MaxSpeakerLabels": MAX_SPEAKERS,
            "ShowAlternatives": False,
        },
    )

    return _response(200, {"job_name": job_name})


def _handle_transcribe_status(event):
    payload = _read_json_body(event)
    job_name = payload.get("job_name")
    if not job_name:
        return _response(400, {"error": "Missing 'job_name'."})

    job = transcribe.get_transcription_job(TranscriptionJobName=job_name)
    tj = job["TranscriptionJob"]
    status = tj["TranscriptionJobStatus"]

    if status == "FAILED":
        return _response(200, {"status": "FAILED", "error": tj.get("FailureReason", "Unknown failure")})

    if status != "COMPLETED":
        return _response(200, {"status": status})

    transcript_url = tj["Transcript"]["TranscriptFileUri"]

    with urllib.request.urlopen(transcript_url) as resp:
        transcript_json = json.loads(resp.read().decode("utf-8"))

    transcript_text = (
        transcript_json.get("results", {})
        .get("transcripts", [{}])[0]
        .get("transcript", "")
    )
    transcript_text = _dedupe_text(transcript_text)

    # Save JSON for inspection/debugging
    transcript_key = f"transcripts/{job_name}.json"
    s3.put_object(
        Bucket=TRANSCRIPT_BUCKET,
        Key=transcript_key,
        Body=json.dumps(transcript_json).encode("utf-8"),
        ContentType="application/json",
    )

    speaker_count, speaker_lines = _speaker_turns_from_transcribe_json(transcript_json)

    return _response(
        200,
        {
            "status": "COMPLETED",
            "transcript": transcript_text,
            "speaker_count": speaker_count,
            "speaker_lines": speaker_lines,
        },
    )


def lambda_handler(event, context):
    # CORS preflight
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {"ok": True})

    path = _get_path(event)

    try:
        if path.endswith("/presign"):
            return _handle_presign(event)
        if path.endswith("/transcribe"):
            return _handle_transcribe_start(event)
        if path.endswith("/status"):
            return _handle_transcribe_status(event)
        return _response(404, {"error": f"Unknown route: {path}. Use /presign, /transcribe, /status."})
    except Exception as e:
        return _response(500, {"error": str(e)})
