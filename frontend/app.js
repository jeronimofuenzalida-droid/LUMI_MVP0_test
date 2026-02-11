const API_URL = "PASTE_API_GATEWAY_INVOKE_URL_HERE/transcribe";

const fileInput = document.getElementById("file");
const btn = document.getElementById("btn");
const statusEl = document.getElementById("status");
const transcriptEl = document.getElementById("transcript");

fileInput.addEventListener("change", () => {
  btn.disabled = !(fileInput.files && fileInput.files.length);
});

function setStatus(msg) { statusEl.textContent = msg; }

btn.addEventListener("click", async () => {
  const file = fileInput.files[0];
  if (!file) return;

  transcriptEl.textContent = "—";
  setStatus("Reading file...");

  const reader = new FileReader();
  reader.onload = async () => {
    try {
      setStatus("Sending to backend...");
      const base64 = reader.result.split(",")[1];

      const res = await fetch(API_URL, {
        method: "POST",
        headers: { "Content-Type": "applicationdate & Time: configured (you already fixed this earlier)  

If you answer those, I’ll give you the exact Lambda+API Gateway steps next.
