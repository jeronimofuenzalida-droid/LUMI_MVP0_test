const API_BASE = "https://sh6ceul3xa.execute-api.us-east-2.amazonaws.com";

const fileInput = document.getElementById("file");
const btn = document.getElementById("btn");
const statusEl = document.getElementById("status");
const speakersEl = document.getElementById("speakers");
const dialogueEl = document.getElementById("dialogue");

let inFlight = false;

function setStatus(msg) {
  statusEl.textContent = msg;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;"
  }[c]));
}

function setBusy(isBusy) {
  inFlight = isBusy;
  btn.disabled = isBusy || !(fileInput.files && fileInput.files.length);
  fileInput.disabled = isBusy;
}

fileInput.addEventListener("change", () => {
  if (!inFlight) btn.disabled = !(fileInput.files && fileInput.files.length);
});

btn.addEventListener("click", async () => {
  if (inFlight) return;

  const file = fileInput.files[0];
  if (!file) return;

  // Reset UI for a clean run
  speakersEl.textContent = "—";
  dialogueEl.textContent = "—";

  setBusy(true);

  try {
    setStatus("Requesting upload URL...");

    // 1) Presign
    const presignRes = await fetch(`${API_BASE}/presign`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name,
        content_type: file.type || "application/octet-stream"
      })
    });

    const presignData = await presignRes.json();
    if (!presignRes.ok) {
      setStatus("Error.");
      dialogueEl.textContent = presignData.error || "Presign failed";
      return;
    }

    const { upload_url, s3_key } = presignData;

    // 2) Upload to S3
    setStatus("Uploading to S3...");
    const putRes = await fetch(upload_url, {
      method: "PUT",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file
    });

    if (!putRes.ok) {
      setStatus("Error.");
      dialogueEl.textContent = `S3 upload failed (${putRes.status})`;
      return;
    }

    // 3) Transcribe
    setStatus("Transcribing (with diarization)...");

    const txRes = await fetch(`${API_BASE}/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ s3_key })
    });

    const data = await txRes.json();
    if (!txRes.ok) {
      setStatus("Error.");
      dialogueEl.textContent = data.error || "Transcription failed";
      return;
    }

    // Render
    speakersEl.textContent = `Speakers detected: ${data.speaker_count ?? 0}`;

    if (Array.isArray(data.speaker_lines) && data.speaker_lines.length) {
      dialogueEl.innerHTML = data.speaker_lines.map((x) => {
        const sp = escapeHtml(x.speaker || "speaker");
        const tx = escapeHtml(x.text || "");
        return `<div class="line"><span class="spk">${sp}:</span> ${tx}</div>`;
      }).join("");
    } else {
      dialogueEl.textContent = data.transcript || "No transcript returned";
    }

    setStatus("Done.");
  } catch (e) {
    setStatus("Error.");
    dialogueEl.textContent = e?.message || "Unknown error";
  } finally {
    setBusy(false);
  }
});
