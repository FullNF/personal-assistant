import os
import re
import uuid
import shutil
import threading
import time

from flask import Flask, request, jsonify, send_file, after_this_request
from flask_cors import CORS
import yt_dlp

app = Flask(__name__, static_folder="static", static_url_path="")

# Restrict this to your Vercel frontend URL in production, e.g.
# CORS(app, resources={r"/api/*": {"origins": "https://your-app.vercel.app"}})
CORS(app, resources={r"/api/*": {"origins": os.environ.get("ALLOWED_ORIGIN", "*")}})

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_ROOT = os.path.join(BASE_DIR, "downloads")
os.makedirs(DOWNLOAD_ROOT, exist_ok=True)

# On hosts without a system ffmpeg (e.g. Render's Python runtime), the build
# step downloads a static ffmpeg/ffprobe binary next to this file.
_LOCAL_FFMPEG = os.path.join(BASE_DIR, "ffmpeg")
FFMPEG_LOCATION = BASE_DIR if os.path.exists(_LOCAL_FFMPEG) else None

# Cloud/datacenter IPs (Render, AWS, etc.) are frequently bot-checked by
# YouTube on the default web client. The android/ios player clients skip
# that check for most videos.
# yt-dlp rewrites the cookie jar in place (updated expiry/session values), so
# a read-only mount (e.g. Render's /etc/secrets) must be copied to a writable
# path first.
_SOURCE_COOKIES_FILE = os.environ.get("YTDLP_COOKIES_FILE")
COOKIES_FILE = None
if _SOURCE_COOKIES_FILE and os.path.exists(_SOURCE_COOKIES_FILE):
    COOKIES_FILE = os.path.join(BASE_DIR, "cookies_runtime.txt")
    shutil.copyfile(_SOURCE_COOKIES_FILE, COOKIES_FILE)

# With real account cookies, the plain "web" client behaves like a logged-in
# browser and avoids the bot check. Without cookies, fall back to android/ios
# which historically skip that check on cloud/datacenter IPs.
EXTRACTOR_ARGS = {"youtube": {"player_client": ["web"] if COOKIES_FILE else ["android", "ios", "web"]}}


def safe_filename(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name[:150] if name else "download"


@app.route("/")
def index():
    return app.send_static_file("index.html")


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/debug")
def debug():
    info = {
        "ffmpeg_location": FFMPEG_LOCATION,
        "source_cookies_env": _SOURCE_COOKIES_FILE,
        "source_cookies_exists": bool(_SOURCE_COOKIES_FILE and os.path.exists(_SOURCE_COOKIES_FILE)),
        "cookies_file": COOKIES_FILE,
        "cookies_file_exists": bool(COOKIES_FILE and os.path.exists(COOKIES_FILE)),
        "cookies_file_size": os.path.getsize(COOKIES_FILE) if COOKIES_FILE and os.path.exists(COOKIES_FILE) else 0,
        "yt_dlp_version": yt_dlp.version.__version__,
    }
    return jsonify(info)


@app.route("/api/info", methods=["POST"])
def info():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "extractor_args": EXTRACTOR_ARGS,
    }
    if FFMPEG_LOCATION:
        ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"error": f"Could not read video info: {e}"}), 400

    formats = result.get("formats", []) or []

    seen_heights = {}
    for f in formats:
        if f.get("vcodec") in (None, "none"):
            continue
        height = f.get("height")
        if not height:
            continue
        current = seen_heights.get(height)
        if current is None or (f.get("tbr") or 0) > (current["tbr"] or 0):
            seen_heights[height] = {
                "format_id": f["format_id"],
                "height": height,
                "ext": f.get("ext"),
                "tbr": f.get("tbr") or 0,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            }

    video_options = sorted(seen_heights.values(), key=lambda x: x["height"], reverse=True)

    return jsonify({
        "title": result.get("title"),
        "thumbnail": result.get("thumbnail"),
        "duration": result.get("duration"),
        "uploader": result.get("uploader"),
        "video_options": video_options,
    })


def cleanup_later(path, delay=120):
    def _remove():
        time.sleep(delay)
        try:
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass
    threading.Thread(target=_remove, daemon=True).start()


@app.route("/api/download", methods=["POST"])
def download():
    data = request.get_json(force=True) or {}
    url = (data.get("url") or "").strip()
    mode = data.get("mode")  # "mp3" or "video"
    format_id = data.get("format_id")  # required if mode == "video"

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if mode not in ("mp3", "video"):
        return jsonify({"error": "Invalid mode"}), 400
    if mode == "video" and not format_id:
        return jsonify({"error": "format_id required for video mode"}), 400

    job_id = uuid.uuid4().hex
    job_dir = os.path.join(DOWNLOAD_ROOT, job_id)
    os.makedirs(job_dir, exist_ok=True)

    outtmpl = os.path.join(job_dir, "%(title)s.%(ext)s")

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": outtmpl,
        "restrictfilenames": False,
        "extractor_args": EXTRACTOR_ARGS,
    }
    if FFMPEG_LOCATION:
        ydl_opts["ffmpeg_location"] = FFMPEG_LOCATION
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE

    if mode == "mp3":
        ydl_opts["format"] = "bestaudio/best"
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    else:
        ydl_opts["format"] = f"{format_id}+bestaudio/best/{format_id}"
        ydl_opts["merge_output_format"] = "mp4"

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": f"Download failed: {e}"}), 500

    files = os.listdir(job_dir)
    if not files:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "No file produced"}), 500

    result_file = os.path.join(job_dir, files[0])
    download_name = safe_filename(files[0])

    @after_this_request
    def _cleanup(response):
        cleanup_later(job_dir, delay=60)
        return response

    return send_file(result_file, as_attachment=True, download_name=download_name)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
