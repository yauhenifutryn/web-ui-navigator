const bootstrapMessage = document.getElementById("bootstrap-message");
const retryButton = document.getElementById("retry-bootstrap-btn");

async function fetchJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(`${url} -> ${response.status} ${text}`);
  }
  return response.json();
}

async function bootstrapOverlay() {
  bootstrapMessage.textContent = "Connecting the overlay to your controlled Chrome tab.";
  const payload = await fetchJSON("/api/bootstrap-overlay", { method: "POST" });
  if (payload && payload.ok === false) {
    bootstrapMessage.textContent = payload.message || "Open a target website tab in the controlled Chrome window first, then retry the overlay.";
    return;
  }
  bootstrapMessage.textContent = "Overlay connected. Move back to the controlled website tab and use the right-side rail there.";
}

retryButton?.addEventListener("click", () => {
  bootstrapOverlay().catch((err) => {
    bootstrapMessage.textContent = err.message;
  });
});

bootstrapOverlay().catch((err) => {
  bootstrapMessage.textContent = err.message;
});
