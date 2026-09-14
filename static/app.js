const form = document.querySelector("#download-form");
const urlInput = document.querySelector("#media-url");
const qualityInput = document.querySelector("#quality");
const submitButton = document.querySelector("#submit-button");
const sourceBadge = document.querySelector("#source-badge");
const formError = document.querySelector("#form-error");
const health = document.querySelector("#health");
const emptyState = document.querySelector("#empty-state");
const jobList = document.querySelector("#job-list");
const queueCount = document.querySelector("#queue-count");
const template = document.querySelector("#job-template");
const logoutButton = document.querySelector("#logout-button");

let jobs = [];
let pollTimer;

function detectSource(value) {
  try {
    const parsed = new URL(value);
    const host = parsed.hostname.toLowerCase();
    if (host === "open.spotify.com") return "spotify";
    if (["youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"].includes(host)) {
      return "youtube";
    }
  } catch {
    return null;
  }
  return null;
}

function updateSourceBadge() {
  const source = detectSource(urlInput.value.trim());
  sourceBadge.hidden = !source;
  sourceBadge.textContent = source || "";
  sourceBadge.className = `source-badge${source ? ` ${source}` : ""}`;
}

function setError(message = "") {
  formError.textContent = message;
  formError.hidden = !message;
}

function bytesLabel(bytes) {
  if (!Number.isFinite(bytes) || bytes < 1) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const power = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  const value = bytes / 1024 ** power;
  return `${value >= 10 || power === 0 ? value.toFixed(0) : value.toFixed(1)} ${units[power]}`;
}

function makeDownloadLink(label, href, primary = false) {
  const link = document.createElement("a");
  link.className = `download-link${primary ? " primary" : ""}`;
  link.href = href;
  link.textContent = label;
  return link;
}

function renderJobs() {
  jobList.replaceChildren();
  emptyState.hidden = jobs.length > 0;
  queueCount.textContent = `${jobs.length} job${jobs.length === 1 ? "" : "s"}`;

  for (const job of jobs) {
    const fragment = template.content.cloneNode(true);
    const card = fragment.querySelector(".job-card");
    const icon = fragment.querySelector(".source-icon");
    const details = fragment.querySelector(".job-details");
    const actions = fragment.querySelector(".job-actions");

    card.dataset.status = job.status;
    icon.classList.add(job.source);
    icon.textContent = job.source === "spotify" ? "S" : "YT";
    fragment.querySelector(".job-source").textContent = job.source;
    fragment.querySelector(".job-status").textContent = job.status;
    fragment.querySelector(".job-url").textContent = job.url;
    fragment.querySelector(".job-url").title = job.url;
    fragment.querySelector(".job-message").textContent = job.message;
    fragment.querySelector(".job-quality").textContent = `${job.quality} kbps`;
    fragment.querySelector(".job-progress").value = job.progress || 0;

    const files = Array.isArray(job.files) ? job.files : [];
    if (job.status === "complete" && files.length) {
      if (files.length > 1) {
        actions.append(makeDownloadLink(`Download all · ${files.length} files`, `/api/jobs/${job.id}/archive`, true));
      }
      for (const file of files.slice(0, 4)) {
        actions.append(makeDownloadLink(`${file.name} · ${bytesLabel(file.size)}`, file.url, files.length === 1));
      }
      if (files.length > 4) {
        const more = document.createElement("span");
        more.className = "download-link";
        more.textContent = `+ ${files.length - 4} more in ZIP`;
        actions.append(more);
      }
    }

    const log = Array.isArray(job.log) ? job.log : [];
    if (log.length) {
      fragment.querySelector(".job-log").textContent = log.join("\n");
    } else {
      details.hidden = true;
    }

    jobList.append(fragment);
  }
}

async function loadHealth() {
  try {
    const response = await fetch("/api/health", { cache: "no-store" });
    if (!response.ok) throw new Error("Server unavailable");
    const data = await response.json();
    const ready = data.tools.youtube && data.tools.spotify && data.tools.ffmpeg;
    health.className = `health ${ready ? "ready" : "warning"}`;
    health.querySelector("span:last-child").textContent = ready ? "Ready to download" : "Setup needs attention";
  } catch {
    health.className = "health warning";
    health.querySelector("span:last-child").textContent = "Server unavailable";
  }
}

async function loadJobs() {
  try {
    const response = await fetch("/api/jobs", { cache: "no-store" });
    if (!response.ok) throw new Error("Could not load jobs");
    const data = await response.json();
    jobs = data.jobs || [];
    renderJobs();

    const active = jobs.some((job) => ["queued", "running"].includes(job.status));
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(loadJobs, active ? 1500 : 6000);
  } catch {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(loadJobs, 6000);
  }
}

urlInput.addEventListener("input", updateSourceBadge);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  setError();
  const url = urlInput.value.trim();
  if (!detectSource(url)) {
    setError("Paste a full Spotify or YouTube link beginning with https://");
    urlInput.focus();
    return;
  }

  submitButton.disabled = true;
  submitButton.querySelector("span:first-child").textContent = "Adding…";
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, quality: qualityInput.value }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Could not add this download.");
    urlInput.value = "";
    updateSourceBadge();
    await loadJobs();
  } catch (error) {
    setError(error.message || "Could not add this download.");
  } finally {
    submitButton.disabled = false;
    submitButton.querySelector("span:first-child").textContent = "Add to queue";
  }
});

loadHealth();
loadJobs();
window.setInterval(loadHealth, 15000);

logoutButton.addEventListener("click", async () => {
  logoutButton.disabled = true;
  try {
    await fetch("/api/logout", { method: "POST" });
  } finally {
    window.location.replace("/login");
  }
});
