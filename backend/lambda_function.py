import os
import json
import base64
import uuid
import time
import urllib.request
import boto3

s3 = boto3.client("s3")
transcribe = boto3.client("transcribe")

AUDIO_BUCKET = os.environ["AUDIO_BUCKET"]
TRANSCRIPT_BUCKET = os.environ.get("TRANSCRIPT_BUCKET", AUDIO_BUCKET)
LANGUAGE_CODE = os.environ.get("LANGUAGE_CODE", "en-US")

PRESIGN_EXPIRES_SECONDS = int(os.environ.get("PRESIGN_EXPIRES_SECONDS", "900"))  # 15 min

def _response(status_code: int, body: dict):
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "content-type",
            "Access-Control-Allow-Methods": "OPTIONS,GET,POST",
        },
        "body": json.dumps(body),
    }

def _get_method_path(event):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
        or ""
    )
    path = event.get("rawPath") or event.get("path") or ""
    return method.upper(), path

def _get_json_body(event):
    body_raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body_raw = base64.b64decode(body_raw).decode("utf-8", errors="replace")
    if not body_raw:
        return {}
    return json.loads(body_raw)

def _safe_ext(filename: str) -> str:
    ext = (filename.split(".")[-1] or "").lower()
    if ext not in {"mp3", "wav", "m4a"}:
        raise ValueError(f"Unsupported file type: .{ext}. Use mp3/wav/m4a.")
    return ext

def lambda_handler(event, context):
    method, path = _get_method_path(event)

    # CORS preflight
    if method == "OPTIONS":
        return _response(200, {"ok": True})

    try:
        # ---------- /presign ----------
        if path.endswith("/presign") and method == "POST":
            payload = _get_json_body(event)
            filename = payload.get("filename", "audio.mp3")
            content_type = payload.get("contentType") or "application/octet-stream"

            ext = _safe_ext(filename)
            key = f"uploads/{uuid.uuid4().hex}.{ext}"

            upload_url = s3.generate_presigned_url(
                ClientMethod="put_object",
                Params={
                    "Bucket": AUDIO_BUCKET,
                    "Key": key,
                    "ContentType": content_type,
                },
                ExpiresIn=PRESIGN_EXPIRES_SECONDS,
            )

            return _response(200, {"uploadUrl": upload_url, "key": key, "bucket": AUDIO_BUCKET})

        # ---------- /transcribe ----------
        # Starts an async Transcribe job and returns jobName
        if path.endswith("/transcribe") and method == "POST":
            payload = _get_json_body(event)

            # Expect S3 key now
            key = payload.get("key")
            filename = payload.get("filename", "audio.mp3")
            if not key:
                return _response(400, {"error": "Missing 'key'. Upload to S3 via /presign first."})

            # (Optional) validate extension for safety
            _safe_ext(filename)

            media_uri = f"s3://{AUDIO_BUCKET}/{key}"
            job_name = f"lumi-{uuid.uuid4().hex}"

            transcribe.start_transcription_job(
                TranscriptionJobName=job_name,
                LanguageCode=LANGUAGE_CODE,
                Media={"MediaFileUri": media_uri},
                Settings={"ShowSpeakerLabels": False, "ShowAlternatives": False},
            )

            return _response(202, {"jobName": job_name})

        # ---------- /status ----------
        # Poll job status; when complete, fetch transcript, store JSON to S3, return transcript text
        if path.endswith("/status") and method == "GET":
            qs = event.get("queryStringParameters") or {}
            job_name = qs.get("jobName")
            if not job_name:
                return _response(400, {"error": "Missing jobName query param."})

            job = transcribe.get_transcription_job(TranscriptionJobName=job_name)
            status = job["TranscriptionJob"]["TranscriptionJobStatus"]

            if status == "FAILED":
                reason = job["TranscriptionJob"].get("FailureReason", "Unknown failure")
                return _response(200, {"status": status, "error": reason})

            if status != "COMPLETED":
                return _response(200, {"status": status})

            transcript_url = job["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]

            with urllib.request.urlopen(transcript_url) as resp:
                transcript_json = json.loads(resp.read().decode("utf-8"))

            transcript_text = (
                transcript_json.get("results", {})
                .get("transcripts", [{}])[0]
                .get("transcript", "")
            )

            # Persist transcript JSON
            transcript_key = f"transcripts/{job_name}.json"
            s3.put_object(
                Bucket=TRANSCRIPT_BUCKET,
                Key=transcript_key,
                Body=json.dumps(transcript_json).encode("utf-8"),
                ContentType="application/json",
            )

            return _response(200, {"status": status, "transcript": transcript_text, "transcriptKey": transcript_key})

        return _response(404, {"error": "Not found"})

    except Exception as e:
        return _response(500, {"error": str(e)})
