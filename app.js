// IMPORTANT: keep your correct API endpoint here
const API_URL = "https://sh6ceul3xa.execute-api.us-east-2.amazonaws.com/transcribe";

const fileInput = document.getElementById("file");
const btn = document.getElementById("btn");
const statusEl = document.getElementById("status");
const speakersEl = document.getElementById("speakers");
const dialogueEl = document.getElementById("dialogue");

fileInput.addEventListener("change", () => {
  btn.disabled = !(fileInput.files && fileInput.files.length);
});

function setStatus(msg) {
  statusEl.textContent = msg;
}

function escapeHtml(str) {
  return String(str).replace(/[&<>"']/g, function (c) {
    return {
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;"
    }[c];
  });
}

btn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  dialogueEl.textContent = "—";
  speakersEl.textContent = "—";
  setStatus("Reading file...");

  const reader = new FileReader();

  reader.onload = async () => {
    try {
      setStatus("Sending to backend...");

      const base64 = reader.result.split(",")[1];

      const response = await fetch(API_URL, {
        method: "POST",
        headers: {
          "Content-Type": "application/json"
        },
        body: JSON.stringify({
          filename: file.name,
          audio: base64
        })
      });

      const data = await response.json();

      if (!response.ok) {
        setStatus("Error.");
        dialogueEl.textContent = data.error || "Request failed";
        return;
      }

      // Show number of speakers first
      const count = data.speaker_count ?? 0;
      speakersEl.textContent = `Speakers detected: ${count}`;

      // Render speaker lines
      if (Array.isArray(data.speaker_lines) && data.speaker_lines.length) {
        const html = data.speaker_lines.map(line => {
          const speaker = escapeHtml(line.speaker || "speaker");
          const text = escapeHtml(line.text || "");
          return `<div class="line"><span class="spk">${speaker}:</span> ${text}</div>`;
        }).join("");

        dialogueEl.innerHTML = html;
      } else {
        // fallback to full transcript if diarization missing
        dialogueEl.textContent = data.transcript || "No transcript returned";
      }

      setStatus("Done.");

    } catch (err) {
      setStatus("Error: " + err.message);
    }
  };

  reader.readAsDataURL(file);
});
