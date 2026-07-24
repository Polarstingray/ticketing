// Uses the global Tauri API (app.withGlobalTauri = true in tauri.conf.json) so
// this stays a plain static page with no bundler/build step.
const { invoke } = window.__TAURI__.core;

const form = document.getElementById("connect-form");
const input = document.getElementById("server-url");
const btn = document.getElementById("connect-btn");
const errorEl = document.getElementById("error");

function showError(msg) {
  errorEl.textContent = msg;
  errorEl.hidden = false;
}

function clearError() {
  errorEl.hidden = true;
  errorEl.textContent = "";
}

// Prefill with the last server the Rust side had on file (e.g. after the user
// picked "Switch server…"), so re-connecting is one click.
invoke("last_server_url")
  .then((url) => {
    if (url && !input.value) input.value = url;
  })
  .catch(() => {});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();

  const raw = input.value.trim();
  if (!raw) {
    showError("Enter your server's address.");
    return;
  }

  btn.disabled = true;
  btn.textContent = "Connecting…";

  try {
    // Rust normalizes the URL, verifies the server is reachable and looks like a
    // Stingray instance, persists it, then swaps this window for the app.
    await invoke("connect_to_server", { url: raw });
  } catch (err) {
    showError(typeof err === "string" ? err : "Could not reach that server.");
    btn.disabled = false;
    btn.textContent = "Connect";
    input.focus();
  }
});
