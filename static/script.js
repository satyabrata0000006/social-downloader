// static/script.js
function cleanUniversalUrl(url) {
  try {
    const u = new URL(url);
    if (u.hostname === "youtu.be")
      return `https://www.youtube.com/watch?v=${u.pathname.slice(1)}`;
    if (u.hostname.includes("youtube.com")) {
      if (u.pathname.startsWith("/shorts/"))
        return `https://www.youtube.com/watch?v=${u.pathname.split("/shorts/")[1].split("/")[0]}`;
      if (u.searchParams.get("v"))
        return `https://www.youtube.com/watch?v=${u.searchParams.get("v")}`;
    }
    return url.split("?")[0];
  } catch {
    return url;
  }
}

function humanFileSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  const thresh = 1024;
  if (Math.abs(bytes) < thresh) return bytes + " B";
  const units = ["KB","MB","GB","TB"];
  let u=-1;
  do { bytes /= thresh; ++u; } while (Math.abs(bytes) >= thresh && u < units.length-1);
  return bytes.toFixed(1) + " " + units[u];
}

const urlInput = document.getElementById("urlInput");
const getInfoBtn = document.getElementById("getInfoBtn");
const autoCookieBtn = document.getElementById("autoCookieBtn");
const infoSection = document.getElementById("infoSection");
const thumb = document.getElementById("thumb");
const videoTitle = document.getElementById("videoTitle");
const metaRow = document.getElementById("metaRow");
const formatSelect = document.getElementById("formatSelect");
const audioSelect = document.getElementById("audioSelect");
const downloadBtn = document.getElementById("downloadBtn");
const progressBar = document.getElementById("progressBar");
const progressInner = document.getElementById("progressInner");
const statusText = document.getElementById("statusText");
const clearBtn = document.getElementById("clearBtn");
const cookiesFile = document.getElementById("cookiesFile");
const useBrowserCookies = document.getElementById("useBrowserCookies");
const autoCookieBtnEl = document.getElementById("autoCookieBtn");
const liveLog = document.getElementById("liveLog");
const hintArea = document.getElementById("hintArea");
const hintText = document.getElementById("hintText");

function showSpinner(btn, text) { btn.innerHTML = `<span class="spinner"></span> ${text}`; }
function resetButton(btn, text) { btn.innerHTML = text; }

autoCookieBtnEl.addEventListener("click", () => {
  useBrowserCookies.checked = !useBrowserCookies.checked;
  autoCookieBtnEl.classList.toggle("bg-green-500", useBrowserCookies.checked);
  autoCookieBtnEl.classList.toggle("text-white", useBrowserCookies.checked);
});

function appendLog(msg) {
  const ts = new Date().toLocaleTimeString();
  const line = document.createElement("div");
  line.textContent = `[${ts}] ${msg}`;
  liveLog.appendChild(line);
  liveLog.scrollTop = liveLog.scrollHeight;
}

function showHintForStatus(status) {
  if (!status) { hintArea.classList.add("hidden"); return; }
  const lower = (status || "").toLowerCase();
  if (lower === "running" || lower === "processing") {
    hintText.innerText = "Background task running — merging/encoding may take time. Copy/remux is used where possible for speed.";
    hintArea.classList.remove("hidden");
  } else if (lower === "queued") {
    hintText.innerText = "Task queued — will start shortly.";
    hintArea.classList.remove("hidden");
  } else {
    hintArea.classList.add("hidden");
  }
}

getInfoBtn.addEventListener("click", async () => {
  let url = urlInput.value.trim();
  if (!url) return alert("Please paste a URL!");
  url = cleanUniversalUrl(url);

  getInfoBtn.disabled = true;
  showSpinner(getInfoBtn, "Fetching...");
  statusText.innerText = "Fetching info...";

  try {
    const fd = new FormData();
    fd.append("url", url);
    if (cookiesFile.files && cookiesFile.files.length) fd.append("cookies", cookiesFile.files[0]);
    fd.append("try_browser_cookies", useBrowserCookies.checked ? "1" : "0");

    const res = await fetch("/info", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "No info");

    infoSection.classList.remove("hidden");
    thumb.src = data.thumbnail || "";
    videoTitle.innerText = data.title || data.id || "Unknown";
    let meta = "";
    if (data.uploader) meta += `By ${data.uploader} `;
    if (data.duration) { const m=Math.floor(data.duration/60); const s=data.duration%60; meta += `• ${m}m ${s}s `; }
    if (data.is_live) meta += "• Live";
    metaRow.innerText = meta;

    formatSelect.innerHTML = "";
    const fmts = data.formats || [];

    const candidates = [];
    for (const f of fmts) {
      const ext = f.ext || "";
      if (!ext) continue;
      candidates.push(f);
    }

    const seen = new Set();
    const uniq = [];
    candidates.sort((a,b) => ((b.height||0)-(a.height||0)) || ((b.abr||0)-(a.abr||0)));
    for (const c of candidates) {
      const key = `${c.ext}-${c.height||0}-${c.abr||0}-${c.fps||0}`;
      if (!seen.has(key)) { seen.add(key); uniq.push(c); }
    }

    for (const f of uniq) {
      const opt = document.createElement("option");
      opt.value = f.format_id;
      let parts = [];
      if (f.height) parts.push(`${f.height}p`);
      else if (f.abr) parts.push(`${f.abr} kbps`);
      else parts.push(f.format_note || "");
      parts.push(`.${f.ext||""}`);
      const v = f.vcodec || "none";
      const a = f.acodec || "none";
      if (v !== "none" && a !== "none") parts.push("• muxed");
      else if (v !== "none" && (a === "none" || !a)) parts.push("• video-only");
      else if (v === "none" && a !== "none") parts.push("• audio-only");
      if (f.filesize || f.filesize_approx) parts.push(`• ${humanFileSize(f.filesize || f.filesize_approx)}`);
      opt.textContent = parts.join(" ");
      formatSelect.appendChild(opt);
    }

    const bestOpt = document.createElement("option");
    bestOpt.value = "";
    bestOpt.textContent = "Best available (auto)";
    formatSelect.insertBefore(bestOpt, formatSelect.firstChild);

    statusText.innerText = "✅ Info loaded";
    appendLog("Info loaded");
  } catch (err) {
    console.error(err);
    alert("Failed to fetch info: " + (err.message || err));
    statusText.innerText = "❌ Error fetching info";
    infoSection.classList.add("hidden");
  } finally {
    getInfoBtn.disabled = false;
    resetButton(getInfoBtn, "Get Info");
  }
});

downloadBtn.addEventListener("click", async () => {
  let raw = urlInput.value.trim();
  if (!raw) return alert("Paste URL");
  const url = cleanUniversalUrl(raw);
  const chosenFormat = formatSelect.value || "";
  const chosenAudioConv = audioSelect.value || "";

  downloadBtn.disabled = true;
  showSpinner(downloadBtn, "Starting...");
  progressBar.classList.remove("hidden");
  progressInner.style.width = "0%";
  statusText.innerText = "Starting download...";
  appendLog("Starting download request...");

  try {
    const fd = new FormData();
    fd.append("url", url);
    if (chosenAudioConv) fd.append("requested", chosenAudioConv);
    else if (chosenFormat) fd.append("requested", chosenFormat);
    else fd.append("requested", "");
    if (cookiesFile.files && cookiesFile.files.length) fd.append("cookies", cookiesFile.files[0]);
    fd.append("try_browser_cookies", useBrowserCookies.checked ? "1" : "0");

    const res = await fetch("/download", { method: "POST", body: fd });
    const data = await res.json();
    if (!data.ok) throw new Error(data.error || "failed to start");
    const task_id = data.task_id;
    appendLog(`Task started: ${task_id}`);
    statusText.innerText = "Download started: " + task_id;

    const poll = setInterval(async () => {
      try {
        const r = await fetch(`/task/${task_id}`);
        const j = await r.json();
        if (!j.ok) { console.error("task error", j); return; }
        const task = j.task || {};
        const st = task.status || "";

        // update live log from task.messages
        const msgs = task.messages || [];
        liveLog.innerHTML = "";
        msgs.forEach(m => {
          const d = new Date(m.ts * 1000);
          const ts = d.toLocaleTimeString();
          const el = document.createElement("div");
          el.textContent = `[${ts}] ${m.text}`;
          liveLog.appendChild(el);
        });
        liveLog.scrollTop = liveLog.scrollHeight;

        showHintForStatus(st);

        if (task.progress) {
          let p = task.progress;
          if (typeof p === "number") p = Math.round(p) + "%";
          progressInner.style.width = p;
          statusText.innerText = `Status: ${st} | Progress: ${p}`;
        } else {
          statusText.innerText = `Status: ${st}`;
        }

        if (st === "done" || task.filename) {
          clearInterval(poll);
          progressInner.style.width = "100%";
          statusText.innerText = "✅ Download complete";
          resetButton(downloadBtn, "Download");
          downloadBtn.disabled = false;
          appendLog("Task completed");
          hintArea.classList.add("hidden");

          const filename = task.filename;
          if (filename) {
            window.location.href = `/download_file/${encodeURIComponent(filename)}`;
          } else {
            alert("Download finished but filename unknown.");
          }
        } else if (st === "error" || task.error) {
          clearInterval(poll);
          alert("❌ Download failed: " + (task.error || "unknown"));
          appendLog("Task error: " + (task.error || "unknown"));
          progressBar.classList.add("hidden");
          resetButton(downloadBtn, "Download");
          downloadBtn.disabled = false;
          hintArea.classList.add("hidden");
        }
      } catch (err) {
        console.error("poll error", err);
      }
    }, 1500);

  } catch (err) {
    alert("Failed to start download: " + (err.message || err));
    resetButton(downloadBtn, "Download");
    downloadBtn.disabled = false;
    progressBar.classList.add("hidden");
    statusText.innerText = "❌ Error starting";
    appendLog("Failed to start download: " + (err.message || err));
  }
});

clearBtn.addEventListener("click", () => {
  urlInput.value = "";
  infoSection.classList.add("hidden");
  formatSelect.innerHTML = "";
  audioSelect.value = "";
  progressBar.classList.add("hidden");
  progressInner.style.width = "0%";
  statusText.innerText = "";
  videoTitle.innerText = "";
  thumb.src = "";
  cookiesFile.value = "";
  useBrowserCookies.checked = false;
  liveLog.innerHTML = "";
  hintArea.classList.add("hidden");
  resetButton(getInfoBtn, "Get Info");
  resetButton(downloadBtn, "Download");
});
