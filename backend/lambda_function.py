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
                out.extend(a)
                i += 2 * n
                removed = True
                break
        if not removed:
            out.append(words[i])
            i += 1

    return " ".join(out)


def _dedupe_text(text: str) -> str:
    t = _dedupe_immediate_repeated_ngrams(text, n_min=2, n_max=12)
    t = _dedupe_immediate_repeated_ngrams(t, n_min=2, n_max=12)
    return _norm_space(t)


def _drop_consecutive_duplicate_lines(lines):
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
# Unique words per speaker
# -------------------------

_WORD_RE = re.compile(r"[A-Za-z']+")

def _unique_words_by_speaker(speaker_lines, sample_limit: int = 30):
    """
    Returns a list like:
    [
      { "speaker": "speaker 1", "unique_word_count": 57, "unique_words_sample": ["about", ...] },
      ...
    ]
    """
    words_map = {}
    for ln in speaker_lines:
        sp = ln.get("speaker") or "speaker 1"
        txt = ln.get("text", "")
        tokens = [w.lower() for w in _WORD_RE.findall(txt)]
        if sp not in words_map:
            words_map[sp] = set()
        words_map[sp].update(tokens)

    result = []
    # keep "speaker 1, speaker 2..." natural order
    def spk_num(s):
        try:
            return int(s.split(" ")[1])
        except Exception:
            return 999999

    for sp in sorted(words_map.keys(), key=spk_num):
        ws = sorted(words_map[sp])
        result.append({
            "speaker": sp,
            "unique_word_count": len(ws),
            "unique_words_sample": ws[:sample_limit],
        })

    return result


# -------------------------
# Speaker turns extraction
# -------------------------

def _speaker_turns_from_transcribe_json(transcript_json: dict):
    results = transcript_json.get("results", {})
    items = results.get("items", [])
    speaker_segments = (results.get("speaker_labels", {}) or {}).get("segments", [])

    if not speaker_segments:
        full_text = (results.get("transcripts", [{}])[0] or {}).get("transcript", "")
        full_text = _dedupe_text(full_text)
        return 1, [{"speaker": "speaker 1", "text": full_text}]

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

            while seg_index < len(segments) and start_time > segments[seg_index]["end"]:
                seg_index += 1
            if seg_index >= len(segments):
                break

            speaker = segments[seg_index]["speaker"]
            word = item["alternatives"][0]["content"]

            if speaker != current_speaker:
                flush()
                current_speaker = speaker

            if not current_text:
                current_text = word
            else:
                current_text += " " + word

        elif typ == "punctuation":
            if current_text:
                punct = item["alternatives"][0]["content"]
                current_text += punct

    flush()

    def spk_key(s):
        try:
            return int(str(s).split("_")[1])
        except Exception:
            return 999999

    unique_raw = sorted({ln["speaker"] for ln in speaker_lines if ln.get("speaker")}, key=spk_key)
    mapping = {sp: f"speaker {i+1}" for i, sp in enumerate(unique_raw)}

    for ln in speaker_lines:
        ln["speaker"] = mapping.get(ln["speaker"], "speaker 1")
        ln["text"] = _dedupe_text(ln.get("text", ""))

    speaker_lines = _drop_consecutive_duplicate_lines(speaker_lines)

    return (len(unique_raw) if unique_raw else 1), speaker_lines


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

    transcript_key = f"transcripts/{job_name}.json"
    s3.put_object(
        Bucket=TRANSCRIPT_BUCKET,
        Key=transcript_key,
        Body=json.dumps(transcript_json).encode("utf-8"),
        ContentType="application/json",
    )

    speaker_count, speaker_lines = _speaker_turns_from_transcribe_json(transcript_json)
    unique_words = _unique_words_by_speaker(speaker_lines, sample_limit=30)

    return _response(
        200,
        {
            "status": "COMPLETED",
            "transcript": transcript_text,
            "speaker_count": speaker_count,
            "speaker_lines": speaker_lines,
            "unique_words": unique_words,  # <-- NEW
        },
    )


def lambda_handler(event, context):
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
