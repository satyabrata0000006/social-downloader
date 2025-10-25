// Clean universal URL
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

// Elements
const urlInput = document.getElementById("urlInput");
const getInfoBtn = document.getElementById("getInfoBtn");
const autoCookieBtn = document.getElementById("autoCookieBtn");
const infoSection = document.getElementById("infoSection");
const thumb = document.getElementById("thumb");
const videoTitle = document.getElementById("videoTitle");
const formatSelect = document.getElementById("formatSelect");
const downloadBtn = document.getElementById("downloadBtn");
const progressBar = document.getElementById("progressBar");
const progressInner = document.getElementById("progressInner");
const statusText = document.getElementById("statusText");
const clearBtn = document.getElementById("clearBtn");

function showSpinner(btn, text) {
  btn.innerHTML = `<span class="spinner"></span> ${text}`;
}
function resetButton(btn, text) {
  btn.innerHTML = text;
}

getInfoBtn.addEventListener("click", async () => {
  let url = urlInput.value.trim();
  if (!url) return alert("Please paste a URL!");
  url = cleanUniversalUrl(url);

  getInfoBtn.disabled = true;
  showSpinner(getInfoBtn, "Fetching...");
  statusText.innerText = "Fetching info...";

  try {
    const res = await fetch("/get_info", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url }),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    infoSection.classList.remove("hidden");
    thumb.src = data.thumbnail;
    videoTitle.innerText = data.title;

    if (data.is_youtube) {
      formatSelect.innerHTML = "";
      formatSelect.classList.remove("hidden");
      data.formats.forEach(f => {
        const opt = document.createElement("option");
        opt.value = f.format_id;
        opt.textContent = `${f.resolution} (${f.ext}) — ${f.filesize}`;
        formatSelect.appendChild(opt);
      });
    } else {
      formatSelect.classList.add("hidden");
    }

    statusText.innerText = "✅ Info loaded!";
  } catch (e) {
    alert("Failed to fetch info: " + e.message);
  } finally {
    getInfoBtn.disabled = false;
    resetButton(getInfoBtn, "Get Info");
  }
});

downloadBtn.addEventListener("click", async () => {
  let url = cleanUniversalUrl(urlInput.value.trim());
  const format = formatSelect.classList.contains("hidden") ? null : formatSelect.value;

  downloadBtn.disabled = true;
  showSpinner(downloadBtn, "Downloading...");
  progressBar.classList.remove("hidden");
  progressInner.style.width = "0%";
  statusText.innerText = "Starting download...";

  const res = await fetch("/download", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url, format }),
  });
  const data = await res.json();
  const task_id = data.task_id;

  const poll = setInterval(async () => {
    const r = await fetch(`/progress/${task_id}`);
    const j = await r.json();

    if (j.progress) {
      progressInner.style.width = j.progress;
      statusText.innerText = `Progress: ${j.progress} | Speed: ${j.speed || "?"}`;
    }
    if (j.done) {
      clearInterval(poll);
      progressInner.style.width = "100%";
      statusText.innerText = "✅ Download complete!";
      downloadBtn.disabled = false;
      resetButton(downloadBtn, "Download");
      setTimeout(() => {
        window.location.href = `/download_file/${task_id}`;
      }, 1000);
    }
    if (j.error) {
      clearInterval(poll);
      alert("❌ Error: " + j.error);
      progressBar.classList.add("hidden");
      downloadBtn.disabled = false;
      resetButton(downloadBtn, "Download");
    }
  }, 1000);
});

clearBtn.addEventListener("click", () => {
  urlInput.value = "";
  infoSection.classList.add("hidden");
  formatSelect.innerHTML = "";
  progressBar.classList.add("hidden");
  progressInner.style.width = "0%";
  statusText.innerText = "";
  videoTitle.innerText = "";
  thumb.src = "";
});
