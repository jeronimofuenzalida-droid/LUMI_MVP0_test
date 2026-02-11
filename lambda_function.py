import os
import json
import time
import base64
import uuid
import urllib.request
import boto3

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")

AUDIO_BUCKET = os.environ["AUDIO_BUCKET"]
LANGUAGE_CODE = os.environ.get("LANGUAGE_CODE", "en-US")  # change if needed
MAX_WAIT_SECONDS = int(os.environ.get("MAX_WAIT_SECONDS", "90"))  # short clips only


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


def handler(event, context):
    # Handle preflight CORS
    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return _response(200, {"ok": True})

    try:
        body_raw = event.get("body") or ""
        if event.get("isBase64Encoded"):
            body_raw = base64.b64decode(body_raw).decode("utf-8", errors="replace")
        payload = json.loads(body_raw)

        filename = payload.get("filename", "audio")
        audio_b64 = payload.get("audio")
        if not audio_b64:
            return _response(400, {"error": "Missing 'audio' (base64) in request body."})

        audio_bytes = base64.b64decode(audio_b64)

        # Infer format from filename
        ext = (filename.split(".")[-1] or "").lower()
        if ext not in {"mp3", "wav", "m4a"}:
            # Transcribe supports many formats, but keep MVP simple
            return _response(400, {"error": f"Unsupported file type: .{ext}. Use mp3/wav/m4a."})

        key = f"uploads/{uuid.uuid4().hex}.{ext}"
        s3.put_object(
            Bucket=AUDIO_BUCKET,
            Key=key,
            Body=audio_bytes,
            ContentType="audio/mpeg" if ext == "mp3" else "audio/wav",
        )

        media_uri = f"s3://{AUDIO_BUCKET}/{key}"
        job_name = f"lumi-{uuid.uuid4().hex}"

        transcribe.start_transcription_job(
            TranscriptionJobName=job_name,
            LanguageCode=LANGUAGE_CODE,
            Media={"MediaFileUri": media_uri},
            Settings={
                "ShowSpeakerLabels": False,   # MVP: no diarization
                "ShowAlternatives": False
            },
        )

        # Poll until job completes (short files only)
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

        # Fetch transcript JSON from AWS-hosted URL
        with urllib.request.urlopen(transcript_url) as resp:
            transcript_json = json.loads(resp.read().decode("utf-8"))

        transcript_text = (
            transcript_json.get("results", {})
            .get("transcripts", [{}])[0]
            .get("transcript", "")
        )

        return _response(200, {"transcript": transcript_text})

    except Exception as e:
        return _response(500, {"error": str(e)})
