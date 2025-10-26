import os
import threading
import uuid
import warnings
from urllib.parse import urlparse, parse_qs
from flask import Flask, render_template, request, jsonify, send_file
import yt_dlp
import browser_cookie3

app = Flask(__name__)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
COOKIE_FILE = os.path.join(DOWNLOAD_DIR, "cookies_auto.txt")

progress_data = {}

# ---------- Smart URL Normalizer ----------
def normalize_url(url: str) -> str:
    """Return a clean standard URL for YouTube, Instagram, Threads, etc."""
    parsed = urlparse(url)

    # --- YouTube Normalization ---
    if "youtu.be" in parsed.netloc:
        vid = parsed.path.strip("/")
        return f"https://www.youtube.com/watch?v={vid}"
    if "youtube.com" in parsed.netloc:
        qs = parse_qs(parsed.query)
        if "v" in qs:
            vid = qs["v"][0]
            return f"https://www.youtube.com/watch?v={vid}"
        if "/shorts/" in parsed.path:
            vid = parsed.path.split("/shorts/")[1].split("/")[0]
            return f"https://www.youtube.com/watch?v={vid}"
        if "/embed/" in parsed.path:
            vid = parsed.path.split("/embed/")[1].split("/")[0]
            return f"https://www.youtube.com/watch?v={vid}"
        return url

    # --- Instagram / Threads / Facebook / LinkedIn / X ---
    if any(p in parsed.netloc for p in ["instagram.com", "threads.net", "facebook.com", "linkedin.com", "x.com", "twitter.com", "fb.watch"]):
        return url.split("?")[0]

    return url


# ---------- Auto Cookie Extraction ----------
def try_auto_extract():
    try:
        cj = browser_cookie3.load()
        cookies = list(cj)
        if not cookies:
            return False, "No cookies found. Try running browser or login first."
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for c in cookies:
                f.write(f"{c.domain}\tTRUE\t{c.path}\t{str(c.secure).upper()}\t0\t{c.name}\t{c.value}\n")
        return True, f"✅ {len(cookies)} cookies exported successfully."
    except PermissionError:
        return False, "Permission denied. Run app as Administrator for auto-cookies."
    except Exception as e:
        print("⚠️ Cookie extract failed:", e)
        return False, str(e)


# ---------- Routes ----------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extract_cookies", methods=["POST"])
def extract_cookies():
    ok, msg = try_auto_extract()
    return jsonify({"ok": ok, "message": msg})


@app.route("/get_info", methods=["POST"])
def get_info():
    data = request.get_json()
    url = data.get("url")
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    clean_url = normalize_url(url)
    domain = urlparse(clean_url).netloc

    warnings.filterwarnings("ignore", category=UserWarning)
    yt_dlp.utils.std_headers["User-Agent"] = "Mozilla/5.0"

    ydl_opts = {
        "quiet": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "cookiefile": COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        "retries": 2,
        "socket_timeout": 10,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url, download=False)
        if not info:
            return jsonify({"error": "No info found. Login or cookie required."}), 400

        # YouTube formats
        if "youtube" in domain:
            formats = []
            for f in info.get("formats", []):
                if f.get("filesize"):
                    mb = round(f["filesize"] / 1024 / 1024, 1)
                    fmt = f.get("format_note") or f.get("resolution") or "unknown"
                    formats.append({
                        "format_id": f["format_id"],
                        "ext": f["ext"],
                        "resolution": fmt,
                        "filesize": f"{mb} MB",
                    })
            return jsonify({
                "title": info.get("title"),
                "thumbnail": info.get("thumbnail"),
                "formats": formats,
                "is_youtube": True
            })

        # Other platforms: auto best format
        else:
            return jsonify({
                "title": info.get("title") or "Media",
                "thumbnail": info.get("thumbnail"),
                "formats": [],
                "is_youtube": False
            })
    except Exception as e:
        return jsonify({"error": f"Failed to fetch info: {e}"}), 500


def run_download(url, format_id, task_id):
    clean_url = normalize_url(url)
    domain = urlparse(clean_url).netloc
    output_template = os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s")

    def hook(d):
        if d["status"] == "downloading":
            progress_data[task_id] = {"progress": d.get("_percent_str", "").strip(), "speed": d.get("_speed_str", "").strip()}
        elif d["status"] == "finished":
            progress_data[task_id] = {"done": True}

    ydl_opts = {
        "progress_hooks": [hook],
        "outtmpl": output_template,
        "cookiefile": COOKIE_FILE if os.path.exists(COOKIE_FILE) else None,
        "quiet": True,
        "no_warnings": True,
        "retries": 2,
        "socket_timeout": 10,
    }

    # YouTube needs format selection
    if "youtube" in domain and format_id:
        ydl_opts["format"] = format_id
    else:
        ydl_opts["format"] = "best"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(clean_url)
        filename = ydl.prepare_filename(info)
        progress_data[task_id]["file"] = filename
    except Exception as e:
        progress_data[task_id] = {"error": str(e)}


@app.route("/download", methods=["POST"])
def download():
    data = request.get_json()
    url, fmt = data.get("url"), data.get("format")
    if not url:
        return jsonify({"error": "Missing URL"}), 400
    task_id = str(uuid.uuid4())
    progress_data[task_id] = {"progress": "0%", "speed": ""}
    threading.Thread(target=run_download, args=(url, fmt, task_id), daemon=True).start()
    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
    return jsonify(progress_data.get(task_id, {}))


@app.route("/download_file/<task_id>")
def download_file(task_id):
    data = progress_data.get(task_id)
    if not data or "file" not in data:
        return "File not ready", 404
    return send_file(data["file"], as_attachment=True)


if __name__ == "__main__":
    print("🚀 Starting Flask Downloader — visit http://127.0.0.1:5000")
    app.run(debug=True)
