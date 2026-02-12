import os
import json
import time
import uuid
import urllib.request

import boto3

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")

AUDIO_BUCKET = os.environ["AUDIO_BUCKET"]
TRANSCRIPT_BUCKET = os.environ.get("TRANSCRIPT_BUCKET", AUDIO_BUCKET)
LANGUAGE_CODE = os.environ.get("LANGUAGE_CODE", "en-US")

MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "180"))
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


def _speaker_turns_from_transcribe_json(transcript_json: dict):
    """
    Returns:
      speaker_count: int
      speaker_lines: [{ "speaker": "speaker 1", "text": "..." }, ...]
    """
    results = transcript_json.get("results", {})
    items = results.get("items", [])
    segments = (results.get("speaker_labels", {}) or {}).get("segments", [])

    full_text = (results.get("transcripts", [{}])[0] or {}).get("transcript", "")

    # Fallback if diarization missing
    if not segments:
        return 1, [{"speaker": "speaker 1", "text": full_text}]

    # Map start_time -> spk label
    time_to_speaker = {}
    for seg in segments:
        sp = seg.get("speaker_label")
        for it in seg.get("items", []):
            st = it.get("start_time")
            if st is not None:
                time_to_speaker[st] = sp

    turns = []
    cur_speaker = None
    cur_tokens = []

    def flush():
        nonlocal cur_tokens, cur_speaker
        if cur_speaker is not None and cur_tokens:
            txt = "".join(cur_tokens).strip()
            if txt:
                turns.append({"speaker": cur_speaker, "text": txt})
        cur_tokens = []

    for it in items:
        typ = it.get("type")
        alt = (it.get("alternatives") or [{}])[0]
        content = alt.get("content", "")

        if typ == "pronunciation":
            st = it.get("start_time")
            sp = time_to_speaker.get(st, cur_speaker)

            if sp != cur_speaker:
                flush()
                cur_speaker = sp

            if not cur_tokens:
                cur_tokens.append(content)
            else:
                cur_tokens.append(" " + content)

        elif typ == "punctuation":
            if cur_tokens:
                cur_tokens[-1] += content

    flush()

    # Stable speaker numbering: spk_0 -> speaker 1, etc.
    def spk_sort_key(s):
        try:
            return int(s.split("_")[1])
        except Exception:
            return 999999

    unique = sorted({t["speaker"] for t in turns if t.get("speaker")}, key=spk_sort_key)
    mapping = {sp: f"speaker {i+1}" for i, sp in enumerate(unique)}

    speaker_lines = [{"speaker": mapping.get(t["speaker"], t["speaker"]), "text": t["text"]} for t in turns]
    speaker_count = len(unique) if unique else 1
    return speaker_count, speaker_lines


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


def _handle_transcribe(event):
    payload = _read_json_body(event)
    s3_key = payload.get("s3_key")
    if not s3_key:
        return _response(400, {"error": "Missing 's3_key'. Call /presign, upload to S3, then call /transcribe."})

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

    deadline = time.time() + MAX_WAIT_SECONDS
    while time.time() < deadline:
        job = transcribe.get_transcription_job(TranscriptionJobName=job_name)
        status = job["TranscriptionJob"]["TranscriptionJobStatus"]
        if status in ("COMPLETED", "FAILED"):
            break
        time.sleep(2)

    job = transcribe.get_transcription_job(TranscriptionJobName=job_name)
    status = job["TranscriptionJob"]["TranscriptionJobStatus"]
    if status != "COMPLETED":
        reason = job["TranscriptionJob"].get("FailureReason", "Transcription did not complete in time.")
        return _response(504, {"error": reason})

    transcript_url = job["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
    with urllib.request.urlopen(transcript_url) as resp:
        transcript_json = json.loads(resp.read().decode("utf-8"))

    transcript_text = (
        transcript_json.get("results", {})
        .get("transcripts", [{}])[0]
        .get("transcript", "")
    )

    # Save JSON for debugging/audit
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
            return _handle_transcribe(event)
        return _response(404, {"error": f"Unknown route: {path}. Use /presign or /transcribe."})
    except Exception as e:
        return _response(500, {"error": str(e)})
