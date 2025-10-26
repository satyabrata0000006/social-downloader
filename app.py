# app.py
import os
import time
import tempfile
import threading
import uuid
import traceback
import socket
import shlex
import subprocess
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template, send_from_directory
import yt_dlp
import html

# Optional browser cookie support
try:
    import browser_cookie3
    BROWSER_COOKIE3_AVAILABLE = True
except Exception:
    BROWSER_COOKIE3_AVAILABLE = False

# App setup
app = Flask(__name__, static_folder="static", template_folder="templates")
app.config['JSON_SORT_KEYS'] = False
DOWNLOAD_DIR = Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

# In-memory tasks store
TASKS = {}
TASK_LOCK = threading.Lock()

# ---------------- Utilities ----------------
def safe_basename(path: str) -> str:
    return Path(path).name

def add_task_message(task_id, text):
    t = TASKS.get(task_id)
    if t is None:
        return
    msgs = t.setdefault("messages", [])
    msgs.append({"ts": int(time.time()), "text": text})

def run_subprocess(cmd, env=None, timeout=None):
    try:
        if isinstance(cmd, str):
            cmd_list = shlex.split(cmd)
        else:
            cmd_list = cmd
        proc = subprocess.run(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, env=env, timeout=timeout)
        return proc.returncode, proc.stdout.decode(errors='ignore'), proc.stderr.decode(errors='ignore')
    except subprocess.TimeoutExpired as e:
        return 124, "", f"timeout: {e}"
    except Exception as e:
        return 1, "", str(e)

def find_file_by_info(info):
    try:
        with yt_dlp.YoutubeDL({}) as ydl:
            prepared = Path(ydl.prepare_filename(info))
        if prepared.exists():
            return prepared
        vid = info.get("id")
        title = info.get("title") or ""
        if vid:
            for p in DOWNLOAD_DIR.iterdir():
                if p.is_file() and vid in p.name:
                    return p
        slug = "".join(c for c in title if (c.isalnum() or c in " _-")).strip().lower().replace(" ", "")
        if slug:
            for p in DOWNLOAD_DIR.iterdir():
                if p.is_file() and slug in p.name.lower().replace(" ", ""):
                    return p
        files = [p for p in DOWNLOAD_DIR.iterdir() if p.is_file()]
        if files:
            files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
            return files[0]
    except Exception:
        app.logger.exception("find_file_by_info failed")
    return None

# ---------------- ffprobe / QuickTime compatibility helpers ----------------
def ffprobe_codecs(path: Path):
    """
    Return dict: {"video": "<vcodec>" or None, "audio": "<acodec>" or None}
    """
    try:
        cmd_v = ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
        rc1, outv, errv = run_subprocess(cmd_v, timeout=10)
        vcodec = outv.strip().splitlines()[0].strip() if rc1 == 0 and outv.strip() else None

        cmd_a = ["ffprobe", "-v", "error", "-select_streams", "a:0",
                 "-show_entries", "stream=codec_name", "-of", "default=noprint_wrappers=1:nokey=1", str(path)]
        rc2, outa, erra = run_subprocess(cmd_a, timeout=10)
        acodec = outa.strip().splitlines()[0].strip() if rc2 == 0 and outa.strip() else None

        return {"video": vcodec, "audio": acodec}
    except Exception:
        return None

def is_quicktime_compatible(codecs: dict):
    """
    Heuristic: QuickTime-friendly if H.264/AVC (avc1/h264) + AAC/mp3.
    """
    if not codecs:
        return False
    v = (codecs.get("video") or "").lower()
    a = (codecs.get("audio") or "").lower()

    if not v:
        # audio-only
        if "aac" in a or "mp3" in a or "mp4a" in a:
            return True
        return False

    if ("h264" in v) or ("avc1" in v) or ("avc" in v):
        if not a:  # video-only h264 is fine
            return True
        if "aac" in a or "mp3" in a or "mp4a" in a:
            return True
        return False

    # vp8/vp9/av1 typically not QuickTime-friendly
    return False

# ---------------- Container choice + remux/re-encode ----------------
def choose_best_container(info):
    vcodec = (info.get("vcodec") or "").lower()
    acodec = (info.get("acodec") or "").lower()
    if vcodec in ("none", "", None) and acodec:
        if "opus" in acodec:
            return "webm"
        if "flac" in acodec:
            return "flac"
        if "mp3" in acodec:
            return "mp3"
        if "aac" in acodec or "mp4a" in acodec:
            return "m4a"
        return "m4a"
    if "avc" in vcodec or "h264" in vcodec or "h.264" in vcodec:
        if "opus" in acodec:
            return "webm"
        return "mp4"
    if "vp9" in vcodec or "vp8" in vcodec:
        return "webm"
    if "av1" in vcodec:
        return "webm"
    return "mp4"

def remux_or_encode(task_id, src_path: Path, desired_ext: str, reencode_on_fail=True):
    """
    1) Try fast remux (ffmpeg -c copy) to desired_ext
    2) If remuxed file not QuickTime-compatible (or remux failed), optionally re-encode to H.264 + AAC mp4
    Returns Path to final file (may be original)
    """
    try:
        add_task_message(task_id, f"Preparing container: target .{desired_ext}")
        cur_ext = src_path.suffix.lstrip(".").lower()
        # If already desired extension, still check codecs
        if cur_ext == desired_ext:
            add_task_message(task_id, f"File already .{cur_ext}; checking codecs for compatibility...")
            codecs = ffprobe_codecs(src_path)
            if codecs and is_quicktime_compatible(codecs):
                add_task_message(task_id, "Already QuickTime-compatible; no action needed.")
                return src_path
            # else continue to remux/re-encode

        # Check ffmpeg availability
        rc, out, err = run_subprocess(["ffmpeg", "-version"])
        if rc != 0:
            add_task_message(task_id, "ffmpeg not available; cannot remux/re-encode. Serving original.")
            app.logger.warning("ffmpeg not available: %s", err)
            return src_path

        # Attempt fast remux (copy)
        base = src_path.stem
        outname = DOWNLOAD_DIR / f"{base}.{desired_ext}"
        i = 1
        while outname.exists():
            outname = DOWNLOAD_DIR / f"{base}-{i}.{desired_ext}"
            i += 1

        add_task_message(task_id, "Attempting fast remux (stream copy) via ffmpeg...")
        cmd = ["ffmpeg", "-y", "-i", str(src_path), "-c", "copy"]
        if desired_ext == "mp4":
            cmd += ["-movflags", "faststart"]
        cmd += [str(outname)]
        app.logger.info("Remux cmd: %s", " ".join(shlex.quote(p) for p in cmd))
        rc1, cout1, cerr1 = run_subprocess(cmd, timeout=300)

        if rc1 == 0 and outname.exists():
            add_task_message(task_id, f"Remuxed to {outname.name}. Verifying codecs...")
            codecs = ffprobe_codecs(outname)
            if codecs and is_quicktime_compatible(codecs):
                add_task_message(task_id, "Remux result QuickTime-compatible.")
                try:
                    src_path.unlink()
                    add_task_message(task_id, f"Removed original {src_path.name}")
                except Exception:
                    add_task_message(task_id, f"Could not remove original {src_path.name} (kept).")
                return outname
            else:
                add_task_message(task_id, "Remuxed file not QuickTime-compatible (codecs mismatch). Will fallback to re-encode if allowed.")
        else:
            add_task_message(task_id, "Fast remux failed or errored; will fallback to re-encode if allowed.")
            app.logger.warning("Remux failed rc=%s stderr=%s", rc1, cerr1)

        if not reencode_on_fail:
            add_task_message(task_id, "Re-encode disabled; returning best available file.")
            if outname.exists():
                return outname
            return src_path

        # Re-encode to H.264 + AAC mp4
        add_task_message(task_id, "Re-encoding to H.264 + AAC (.mp4). This may take long for large files.")
        enc_out = DOWNLOAD_DIR / f"{base}.mp4"
        j = 1
        while enc_out.exists():
            enc_out = DOWNLOAD_DIR / f"{base}-{j}.mp4"
            j += 1

        encode_cmd = [
            "ffmpeg", "-y", "-i", str(outname if outname.exists() else src_path),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "faststart",
            str(enc_out)
        ]
        app.logger.info("Encode cmd: %s", " ".join(shlex.quote(p) for p in encode_cmd))
        rc2, cout2, cerr2 = run_subprocess(encode_cmd, timeout=60*60)
        if rc2 == 0 and enc_out.exists():
            add_task_message(task_id, f"Re-encode successful -> {enc_out.name}")
            try:
                if src_path.exists():
                    src_path.unlink()
                    add_task_message(task_id, f"Removed original {src_path.name}")
            except Exception:
                add_task_message(task_id, f"Could not remove original {src_path.name}")
            try:
                if outname.exists() and outname != enc_out:
                    outname.unlink()
            except Exception:
                pass
            return enc_out
        else:
            add_task_message(task_id, "Re-encode failed. Keeping best available file.")
            app.logger.warning("Encode failed rc=%s stderr=%s", rc2, cerr2)
            if outname.exists():
                return outname
            return src_path

    except Exception as e:
        app.logger.exception("remux_or_encode exception: %s", e)
        add_task_message(task_id, f"Remux/encode exception: {e}")
        return src_path

# ---------------- yt-dlp options ----------------
def prepare_yt_dlp_opts(cookiefile=None, output_template=None, allow_unplayable=False,
                        extra_headers=None, progress_hook=None, format_override=None,
                        audio_convert=None, merge_output_format="mp4"):
    default_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if extra_headers:
        default_headers.update(extra_headers)

    opts = {
        "format": format_override or "bestvideo+bestaudio/best",
        "noplaylist": False,
        "ignoreerrors": False,
        "quiet": True,
        "no_warnings": True,
        "outtmpl": output_template or str(DOWNLOAD_DIR / "%(title)s - %(id)s.%(ext)s"),
        "concurrent_fragment_downloads": 5,
        "fragment_retries": 10,
        "retries": 5,
        "socket_timeout": 60,
        "continuedl": True,
        "http_chunk_size": 1048576,
        "postprocessors": [],
        "nocheckcertificate": True,
        "geo_bypass": True,
        "http_headers": default_headers,
        "merge_output_format": merge_output_format,
        "postprocessor_args": ["-c", "copy", "-movflags", "faststart", "-threads", "2"],
    }

    if audio_convert:
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_convert.get("codec", "mp3"),
            "preferredquality": str(audio_convert.get("quality", 192)),
        }]
        opts.pop("postprocessor_args", None)
        opts.pop("merge_output_format", None)

    if cookiefile:
        opts["cookiefile"] = cookiefile
    if allow_unplayable:
        opts["allow_unplayable_formats"] = True
        opts["age_limit"] = 0
    if progress_hook:
        opts["progress_hooks"] = [progress_hook]

    return opts

def run_ydl_extract(url, opts):
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return {"ok": True, "info": info}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def yt_extract_info(url, cookiefile=None, try_browser_cookies=False):
    attempts = []
    opts = prepare_yt_dlp_opts()
    r1 = run_ydl_extract(url, opts)
    attempts.append(("no_cookies", r1))
    if r1.get("ok"):
        return {"info": r1["info"], "attempts": attempts}
    if cookiefile:
        opts = prepare_yt_dlp_opts(cookiefile=cookiefile)
        r2 = run_ydl_extract(url, opts)
        attempts.append(("user_cookiefile", r2))
        if r2.get("ok"):
            return {"info": r2["info"], "attempts": attempts}
    if try_browser_cookies and BROWSER_COOKIE3_AVAILABLE:
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        tmp.close()
        try:
            ok = export_browser_cookies_for_domain("youtube.com", tmp.name)
        except Exception:
            ok = False
        if ok:
            opts = prepare_yt_dlp_opts(cookiefile=tmp.name)
            r3 = run_ydl_extract(url, opts)
            attempts.append(("browser_cookiefile", r3))
            try:
                os.unlink(tmp.name)
            except Exception:
                pass
            if r3.get("ok"):
                return {"info": r3["info"], "attempts": attempts}
    opts = prepare_yt_dlp_opts(cookiefile=cookiefile, allow_unplayable=True)
    r4 = run_ydl_extract(url, opts)
    attempts.append(("allow_unplayable", r4))
    if r4.get("ok"):
        return {"info": r4["info"], "attempts": attempts}
    return {"error": r4.get("error") or r1.get("error"), "attempts": attempts}

def get_request_param(key, default=None):
    if key in request.form:
        return request.form.get(key)
    try:
        js = request.get_json(silent=True) or {}
        if key in js:
            return js.get(key)
    except Exception:
        pass
    return request.args.get(key, default)

# ---------------- Routes ----------------
@app.route("/", methods=["GET"])
def index():
    try:
        return render_template("index.html")
    except Exception:
        static_index = Path(app.static_folder) / "index.html"
        if static_index.exists():
            return send_from_directory(app.static_folder, "index.html")
        return ("<h3>Index not found. Place templates/index.html or static/index.html</h3>", 200)

@app.route("/favicon.ico")
def favicon():
    static_dir = Path(app.static_folder or "static")
    fav = static_dir / "favicon.ico"
    if fav.exists():
        return send_from_directory(str(static_dir), "favicon.ico")
    return ("", 204)

@app.route("/info", methods=["POST"])
@app.route("/get_info", methods=["POST"])
def info_route():
    url = get_request_param("url")
    if not url:
        return jsonify({"ok": False, "error": "url parameter missing"}), 400

    cookiefile_path = None
    if "cookies" in request.files:
        f = request.files["cookies"]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        f.save(tmp.name)
        cookiefile_path = tmp.name

    try_browser = str(get_request_param("try_browser_cookies", "0")).lower() in ("1", "true", "yes")

    try:
        result = yt_extract_info(url, cookiefile=cookiefile_path, try_browser_cookies=try_browser)
    except Exception as e:
        tb = traceback.format_exc()
        app.logger.exception("Exception in info_route")
        if cookiefile_path:
            try: os.unlink(cookiefile_path)
            except: pass
        return jsonify({"ok": False, "error": "internal_error", "detail": str(e), "trace": tb}), 500

    if cookiefile_path:
        try: os.unlink(cookiefile_path)
        except: pass

    if "info" in result:
        info = result["info"]
        return jsonify({
            "ok": True,
            "id": info.get("id"),
            "title": info.get("title"),
            "uploader": info.get("uploader"),
            "duration": info.get("duration"),
            "is_live": info.get("is_live"),
            "webpage_url": info.get("webpage_url") or url,
            "thumbnail": info.get("thumbnail"),
            "formats": info.get("formats") or [],
            "attempts": result.get("attempts", []),
        }), 200
    else:
        return jsonify({"ok": False, "error": result.get("error", "unknown"), "attempts": result.get("attempts", [])}), 422

@app.route("/download", methods=["POST"])
def download_route():
    url = get_request_param("url")
    if not url:
        return jsonify({"ok": False, "error": "url parameter missing"}), 400

    requested = get_request_param("requested")
    cookiefile_path = None
    if "cookies" in request.files:
        f = request.files["cookies"]
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        f.save(tmp.name)
        cookiefile_path = tmp.name

    try_browser = str(get_request_param("try_browser_cookies", "0")).lower() in ("1", "true", "yes")

    try:
        info_result = yt_extract_info(url, cookiefile=cookiefile_path, try_browser_cookies=try_browser)
    except Exception as e:
        app.logger.exception("Failed to prefetch info for download")
        if cookiefile_path:
            try: os.unlink(cookiefile_path)
            except: pass
        return jsonify({"ok": False, "error": "prefetch_failed", "detail": str(e)}), 500

    fmt_to_use = None
    audio_convert = None

    if "info" in info_result:
        info = info_result["info"]
        formats = info.get("formats") or []
        if requested and requested.startswith("audio:"):
            codec = requested.split(":", 1)[1]
            fmt_to_use = "bestaudio"
            audio_convert = {"codec": codec, "quality": 192}
        elif requested:
            sel = None
            for f in formats:
                if str(f.get("format_id")) == str(requested):
                    sel = f
                    break
            if sel:
                vcodec = sel.get("vcodec")
                acodec = sel.get("acodec")
                if vcodec and vcodec != "none" and (not acodec or acodec == "none"):
                    fmt_to_use = f"{requested}+bestaudio/best"
                else:
                    fmt_to_use = requested
            else:
                fmt_to_use = None
    else:
        fmt_to_use = None
        info = None

    task_id = str(uuid.uuid4())
    with TASK_LOCK:
        TASKS[task_id] = {"status": "queued", "progress": "0%", "url": url, "created": time.time(), "messages": []}

    add_task_message(task_id, "Queued download task")

    def progress_hook(d):
        try:
            tid = task_id
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                downloaded = d.get("downloaded_bytes", 0)
                if total and total > 0:
                    p = int(downloaded * 100 / total)
                    TASKS[tid]["progress"] = f"{p}%"
                else:
                    TASKS[tid]["progress"] = f"{min(int(downloaded/1024), 99)}%"
                TASKS[tid]["speed"] = d.get("speed")
                TASKS[tid]["last_progress_time"] = time.time()
                add_task_message(tid, f"Downloading... {TASKS[tid]['progress']}")
            elif d.get("status") == "finished":
                TASKS[task_id]["progress"] = "100%"
                TASKS[task_id]["status"] = "processing"
                TASKS[task_id]["last_progress_time"] = time.time()
                add_task_message(task_id, "Download finished; processing/merging started")
        except Exception:
            app.logger.exception("progress_hook error")

    def worker(tid, u, cookiefile, try_browser_flag, fmt_str, audio_conv, pre_info):
        TASKS[tid]["status"] = "running"
        TASKS[tid]["last_progress_time"] = time.time()
        add_task_message(tid, "Task started")

        out_template = str(DOWNLOAD_DIR / ("%(title)s - %(id)s.%(ext)s"))

        def attempt_download(format_override, audio_convert_over):
            add_task_message(tid, f"Attempting download (format={format_override or 'auto'})")
            opts = prepare_yt_dlp_opts(cookiefile=cookiefile, output_template=out_template,
                                       progress_hook=progress_hook, format_override=format_override,
                                       audio_convert=audio_convert_over)
            if format_override:
                opts["format"] = format_override
            with yt_dlp.YoutubeDL(opts) as ydl:
                infox = ydl.extract_info(u, download=True)
                add_task_message(tid, "yt-dlp: download & postprocessing finished")
                real_path = find_file_by_info(infox)
                if real_path is None:
                    fn = safe_basename(ydl.prepare_filename(infox))
                    real_path = DOWNLOAD_DIR / fn
                add_task_message(tid, f"Raw saved file: {real_path.name}")

                chosen_ext = choose_best_container(infox)
                add_task_message(tid, f"Chosen container for compatibility: .{chosen_ext}")

                final_path = remux_or_encode(tid, real_path, chosen_ext)
                TASKS[tid].update({
                    "status": "done", "progress": "100%", "filename": final_path.name,
                    "info": {"title": infox.get("title"), "id": infox.get("id")},
                    "last_progress_time": time.time()
                })
                add_task_message(tid, f"Final file ready: {final_path.name}")
                return True

        try:
            if fmt_str:
                try:
                    add_task_message(tid, f"Trying requested format: {fmt_str}")
                    attempt_download(fmt_str, audio_conv)
                    return
                except Exception as e_fmt:
                    app.logger.info("Requested format failed: %s", e_fmt)
                    TASKS[tid]["last_error"] = str(e_fmt)
                    add_task_message(tid, f"Requested format failed: {str(e_fmt)}")

            if try_browser_flag and BROWSER_COOKIE3_AVAILABLE:
                tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
                tmp.close()
                ok = False
                try:
                    ok = export_browser_cookies_for_domain("youtube.com", tmp.name)
                except Exception:
                    ok = False
                if ok:
                    try:
                        add_task_message(tid, "Trying with local browser cookies")
                        attempt_download(None, audio_conv)
                        try: os.unlink(tmp.name)
                        except: pass
                        return
                    except Exception as e2:
                        app.logger.info("browser-cookie retry failed: %s", e2)
                        TASKS[tid]["last_error"] = str(e2)
                        add_task_message(tid, f"Browser-cookie retry failed: {str(e2)}")
                        try: os.unlink(tmp.name)
                        except: pass

            try:
                add_task_message(tid, "Trying default best merge (video+audio)")
                attempt_download(None, audio_conv)
                return
            except Exception as e_best:
                app.logger.info("Default best attempt failed: %s", e_best)
                TASKS[tid]["last_error"] = str(e_best)
                add_task_message(tid, f"Default attempt failed: {str(e_best)}")

            try:
                add_task_message(tid, "Trying fallback 'best' single-file")
                attempt_download("best", audio_conv)
                return
            except Exception as e_last:
                app.logger.exception("Final fallback failed: %s", e_last)
                TASKS[tid].update({"status": "error", "error": str(e_last)})
                add_task_message(tid, f"Final fallback failed: {str(e_last)}")
                return

        except Exception as e:
            app.logger.exception("Download worker exception: %s", e)
            TASKS[tid].update({"status": "error", "error": str(e)})
            add_task_message(tid, f"Worker exception: {str(e)}")
        finally:
            if cookiefile:
                try: os.unlink(cookiefile)
                except: pass

    th = threading.Thread(target=worker, args=(task_id, url, cookiefile_path, try_browser, fmt_to_use, audio_convert, info), daemon=True)
    th.start()

    # Monitor thread
    def monitor():
        while True:
            t = TASKS.get(task_id)
            if not t:
                return
            status = t.get("status")
            if status in ("done", "error"):
                return
            last = t.get("last_progress_time", t.get("created", time.time()))
            default_timeout = 180
            if status == "processing":
                timeout = 900  # 15 minutes for processing; increase if large files often
            else:
                timeout = default_timeout
            if time.time() - last > timeout:
                TASKS[task_id].update({"status": "error", "error": "stalled_download_timeout"})
                add_task_message(task_id, f"Stalled: no progress detected for {int(timeout)} seconds (status={status})")
                return
            time.sleep(5)
    mth = threading.Thread(target=monitor, daemon=True)
    mth.start()

    return jsonify({"ok": True, "task_id": task_id}), 200

@app.route("/task/<task_id>", methods=["GET"])
def task_status(task_id):
    t = TASKS.get(task_id)
    if not t:
        return jsonify({"ok": False, "error": "unknown task id"}), 404
    msgs = t.get("messages", [])[-300:]
    resp = {k: v for k, v in t.items() if k != "messages"}
    resp["messages"] = msgs
    return jsonify({"ok": True, "task": resp}), 200

@app.route("/download_file/<filename>", methods=["GET"])
def serve_file(filename):
    safe_name = Path(filename).name
    safe_path = DOWNLOAD_DIR / safe_name
    if safe_path.exists():
        return send_file(str(safe_path), as_attachment=True)

    try:
        decoded = html.unescape(safe_name)
    except Exception:
        decoded = safe_name

    for tid, t in TASKS.items():
        fn = t.get("filename")
        info = t.get("info") or {}
        vid = info.get("id") if info else None
        if fn and (fn == decoded or decoded in fn):
            candidate = DOWNLOAD_DIR / fn
            if candidate.exists():
                return send_file(str(candidate), as_attachment=True)
        if vid and vid in decoded:
            for p in DOWNLOAD_DIR.iterdir():
                if p.is_file() and vid in p.name:
                    return send_file(str(p), as_attachment=True)

    stem = Path(decoded).stem
    for p in DOWNLOAD_DIR.iterdir():
        if p.is_file() and Path(p.name).stem == stem:
            return send_file(str(p), as_attachment=True)

    target = stem.lower().replace(" ", "")
    for p in DOWNLOAD_DIR.iterdir():
        if p.is_file() and target and target in p.name.lower().replace(" ", ""):
            return send_file(str(p), as_attachment=True)

    return jsonify({"ok": False, "error": "file not found", "filename_checked": str(safe_path)}), 404

@app.errorhandler(404)
def not_found(e):
    return jsonify({"ok": False, "error": "not_found", "detail": str(e)}), 404

@app.errorhandler(500)
def server_error(e):
    tb = traceback.format_exc()
    app.logger.exception("Unhandled exception")
    return jsonify({"ok": False, "error": "internal_server_error", "detail": str(e), "trace": tb}), 500

# Port helpers
def is_port_free(port, host="0.0.0.0"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False

def pick_port(preferred=None, fallback_range=range(5001, 5011)):
    if preferred:
        try:
            p = int(preferred)
            if is_port_free(p):
                return p
        except Exception:
            pass
    for p in fallback_range:
        if is_port_free(p):
            return p
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]

if __name__ == "__main__":
    env_port = os.environ.get("PORT")
    preferred = int(env_port) if env_port and env_port.isdigit() else 5000
    port_to_use = pick_port(preferred=preferred, fallback_range=range(preferred+1, preferred+11))
    host = "0.0.0.0"
    print(f"Starting Flask app on http://127.0.0.1:{port_to_use}  (host {host})")
    app.run(host=host, port=port_to_use, debug=False)
