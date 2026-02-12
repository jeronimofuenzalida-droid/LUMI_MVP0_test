const API_BASE = "https://sh6ceul3xa.execute-api.us-east-2.amazonaws.com"; // keep lowercase id
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

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

btn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  transcriptEl.textContent = "—";
  setStatus("Requesting upload URL...");

  // 1) Get presigned upload URL
  const presignRes = await fetch(PRESIGN_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ filename: file.name, contentType: file.type || "application/octet-stream" }),
  });
  const presignData = await presignRes.json();
  if (!presignRes.ok) {
    setStatus(`Error: ${presignData.error || "presign failed"}`);
    return;
  }

  // 2) Upload file directly to S3
  setStatus("Uploading to S3...");
  const putRes = await fetch(presignData.uploadUrl, {
    method: "PUT",
    headers: { "Content-Type": file.type || "application/octet-stream" },
    body: file,
  });
  if (!putRes.ok) {
    setStatus(`Error: S3 upload failed (${putRes.status})`);
    return;
  }

  // 3) Start transcription
  setStatus("Starting transcription...");
  const startRes = await fetch(TRANSCRIBE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: presignData.key, filename: file.name }),
  });
  const startData = await startRes.json();
  if (!(startRes.status === 202)) {
    setStatus(`Error: ${startData.error || "transcribe start failed"}`);
    return;
  }

  // 4) Poll status
  const jobName = startData.jobName;
  setStatus(`Transcribing... (job: ${jobName})`);

  for (;;) {
    await sleep(2000);
    const stRes = await fetch(`${STATUS_URL}?jobName=${encodeURIComponent(jobName)}`);
    const stData = await stRes.json();

    if (stData.status === "COMPLETED") {
      transcriptEl.textContent = stData.transcript || "No transcript returned";
      setStatus("Done.");
      return;
    }
    if (stData.status === "FAILED") {
      setStatus(`Failed: ${stData.error || "unknown"}`);
      return;
    }
    setStatus(`Transcribing... (${stData.status})`);
  }
});
