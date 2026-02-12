// IMPORTANT: keep the API id lowercase "l" -> sh6ceul3xa
const API_URL = "https://sh6ceul3xa.execute-api.us-east-2.amazonaws.com/transcribe";

const fileInput = document.getElementById("file");
const btn = document.getElementById("btn");
const statusEl = document.getElementById("status");
const speakersEl = document.getElementById("speakers");
const dialogueEl = document.getElementById("dialogue");

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

fileInput.addEventListener("change", () => {
  btn.disabled = !(fileInput.files && fileInput.files.length);
});

btn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  // Reset UI
  speakersEl.textContent = "—";
  dialogueEl.textContent = "—";
  setStatus("Reading file...");

  const reader = new FileReader();

  reader.onload = async () => {
    try {
      setStatus("Sending to backend...");

      const base64 = reader.result.split(",")[1];

      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filename: file.name, audio: base64 })
      });

      const data = await res.json();

      if (!res.ok) {
        setStatus("Error.");
        dialogueEl.textContent = data.error || "Request failed";
        return;
      }

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
      setStatus("Error: " + e.message);
    }
  };

  reader.readAsDataURL(file);
});
