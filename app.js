const API_BASE = "https://sh6ceul3xa.execute-api.us-east-2.amazonaws.com";
const PRESIGN_URL = `${API_BASE}/presign`;
const TRANSCRIBE_URL = `${API_BASE}/transcribe`;
const STATUS_URL = `${API_BASE}/status`;

const fileInput = document.getElementById("file");
const btn = document.getElementById("btn");
const statusEl = document.getElementById("status");
const transcriptEl = document.getElementById("transcript");

fileInput.addEventListener("change", () => {
  btn.disabled = !(fileInput.files && fileInput.files.length);
});

function setStatus(msg) {
  statusEl.textContent = msg;
}

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

btn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  transcriptEl.textContent = "—";

  try {
    setStatus("Getting upload URL...");

    // 1️⃣ Get presigned URL
    const presignRes = await fetch(PRESIGN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name,
        contentType: file.type || "application/octet-stream"
      })
    });

    const presignData = await presignRes.json();

    if (!presignRes.ok) {
      setStatus(`Error: ${presignData.error}`);
      return;
    }

    // 2️⃣ Upload file directly to S3
    setStatus("Uploading to S3...");

    const uploadRes = await fetch(presignData.uploadUrl, {
      method: "PUT",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file
    });

    if (!uploadRes.ok) {
      setStatus(`Upload failed (${uploadRes.status})`);
      return;
    }

    // 3️⃣ Start transcription
    setStatus("Starting transcription...");

    const startRes = await fetch(TRANSCRIBE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: presignData.key,
        filename: file.name
      })
    });

    const startData = await startRes.json();

    if (!startRes.ok) {
      setStatus(`Error: ${startData.error}`);
      return;
    }

    const jobName = star
