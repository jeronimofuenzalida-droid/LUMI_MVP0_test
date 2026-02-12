const API_BASE = "https://sh6ceul3xa.execute-api.us-east-2.amazonaws.com";

const fileInput = document.getElementById("file");
const btn = document.getElementById("btn");
const statusEl = document.getElementById("status");
const speakersEl = document.getElementById("speakers");
const dialogueEl = document.getElementById("dialogue");
const wordsWrap = document.getElementById("wordsWrap");

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

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function renderUniqueWordsTable(uniqueWords) {
  if (!Array.isArray(uniqueWords) || uniqueWords.length === 0) {
    wordsWrap.textContent = "—";
    return;
  }

  const rows = uniqueWords.map(r => {
    const sample = Array.isArray(r.unique_words_sample) ? r.unique_words_sample.join(", ") : "";
    return `
      <tr>
        <td>${escapeHtml(r.speaker || "")}</td>
        <td>${escapeHtml(String(r.unique_word_count ?? 0))}</td>
        <td>${escapeHtml(sample)}</td>
      </tr>
    `;
  }).join("");

  wordsWrap.innerHTML = `
    <table class="wordsTable">
      <thead>
        <tr>
          <th>Speaker</th>
          <th>Unique words</th>
          <th>Sample (first 30)</th>
        </tr>
      </thead>
      <tbody>
        ${rows}
      </tbody>
    </table>
  `;
}

btn.addEventListener("click", async () => {
  if (inFlight) return;

  const file = fileInput.files[0];
  if (!file) return;

  // Reset UI
  speakersEl.textContent = "—";
  dialogueEl.textContent = "—";
  wordsWrap.textContent = "—";

  setBusy(true);

  try {
    setStatus("Requesting upload URL...");

    // 1) presign
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

    // 2) upload to S3
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

    // 3) start transcription
    setStatus("Starting transcription...");
    const startRes = await fetch(`${API_BASE}/transcribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ s3_key })
    });

    const startData = await startRes.json();
    if (!startRes.ok) {
      setStatus("Error.");
      dialogueEl.textContent = startData.error || "Start transcription failed";
      return;
    }

    const jobName = startData.job_name;
    setStatus("Transcribing...");

    // Poll up to 15 minutes
    const deadline = Date.now() + 15 * 60 * 1000;

    while (Date.now() < deadline) {
      const stRes = await fetch(`${API_BASE}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ job_name: jobName })
      });

      const stData = await stRes.json();
      if (!stRes.ok) {
        setStatus("Error.");
        dialogueEl.textContent = stData.error || "Status check failed";
        return;
      }

      if (stData.status === "COMPLETED") {
        speakersEl.textContent = `Speakers detected: ${stData.speaker_count ?? 0}`;

        if (Array.isArray(stData.speaker_lines) && stData.speaker_lines.length) {
          dialogueEl.innerHTML = stData.speaker_lines.map((x) => {
            const sp = escapeHtml(x.speaker || "speaker");
            const tx = escapeHtml(x.text || "");
            return `<div class="line"><span class="spk">${sp}:</span> ${tx}</div>`;
          }).join("");
        } else {
          dialogueEl.textContent = stData.transcript || "No transcript returned";
        }

        renderUniqueWordsTable(stData.unique_words);

        setStatus("Done.");
        return;
      }

      if (stData.status === "FAILED") {
        setStatus("Error.");
        dialogueEl.textContent = stData.error || "Transcription failed";
        return;
      }

      setStatus(`Transcribing... (${stData.status})`);
      await sleep(3000);
    }

    setStatus("Error.");
    dialogueEl.textContent = "Timed out waiting for transcription to complete.";
  } catch (e) {
    setStatus("Error.");
    dialogueEl.textContent = e?.message || "Unknown error";
  } finally {
    setBusy(false);
  }
});
