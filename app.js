const API_BASE = "https://sh6ceul3xa.execute-api.us-east-2.amazonaws.com";
const PRESIGN_URL = `${API_BASE}/presign`;
const TRANSCRIBE_URL = `${API_BASE}/transcribe`;
const STATUS_URL = `${API_BASE}/status`;

const fileInput = document.getElementById("file");
const btn = document.getElementById("btn");
const statusEl = document.getElementById("status");
const transcriptEl = document.getElementById("transcript");

function setStatus(msg) {
  statusEl.textContent = msg;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Ensure change always fires even if user re-selects the same file
fileInput.addEventListener("click", () => {
  fileInput.value = null;
});

fileInput.addEventListener("change", () => {
  btn.disabled = !(fileInput.files && fileInput.files.length);
});

btn.addEventListener("click", async () => {
  const file = fileInput.files && fileInput.files[0];
  if (!file) return;

  transcriptEl.textContent = "—";

  try {
    // 1) Get presigned URL
    setStatus("Getting upload URL...");
    const presignRes = await fetch(PRESIGN_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        filename: file.name,
        contentType: file.type || "application/octet-stream",
      }),
    });

    const presignData = await presignRes.json();
    if (!presignRes.ok) {
      setStatus(`Error (presign): ${presignData.error || "unknown"}`);
      return;
    }

    // 2) Upload to S3
    setStatus("Uploading to S3...");
    const uploadRes = await fetch(presignData.uploadUrl, {
      method: "PUT",
      headers: { "Content-Type": file.type || "application/octet-stream" },
      body: file,
    });

    if (!uploadRes.ok) {
      setStatus(`Error (upload): HTTP ${uploadRes.status}`);
      return;
    }

    // 3) Start transcription
    setStatus("Starting transcription...");
    const startRes = await fetch(TRANSCRIBE_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        key: presignData.key,
        filename: file.name,
      }),
    });

    const startData = await startRes.json();
    if (!startRes.ok) {
      setStatus(`Error (start): ${startData.error || "unknown"}`);
      return;
    }

    const jobName = startData.jobName;
    setStatus(`Transcribing... (${jobName})`);

    // 4) Poll status
    while (true) {
      await sleep(2000);

      const statusRes = await fetch(
        `${STATUS_URL}?jobName=${encodeURIComponent(jobName)}`
      );
      const statusData = await statusRes.json();

      if (statusData.status === "COMPLETED") {
        transcriptEl.textContent = statusData.transcript || "No transcript returned";
        setStatus("Done.");
        return;
      }

      if (statusData.status === "FAILED") {
        setStatus(`Failed: ${statusData.error || "unknown"}`);
        return;
      }

      setStatus(`Status: ${statusData.status}`);
    }
  } catch (err) {
    setStatus(`Error: ${err.message}`);
  }
});
