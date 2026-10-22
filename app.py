
import os
from urllib.parse import urlparse

from flask import Flask, request, jsonify, abort
import yt_dlp

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "")
COOKIES_FILE = "/etc/secrets/cookies.txt"


def check_key():
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        abort(401)


def ydl_options():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
    }

    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    return opts


def valid_youtube_url(url):
    try:
        p = urlparse(url)
        host = (p.hostname or "").lower()
        return (
            p.scheme == "https"
            and host in {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "youtu.be",
                "music.youtube.com",
            }
        )
    except Exception:
        return False


def extract_video(url):
    if not valid_youtube_url(url):
        raise ValueError("Invalid YouTube URL")

    with yt_dlp.YoutubeDL(ydl_options()) as ydl:
        info = ydl.extract_info(url, download=False)

    if not info or info.get("_type") == "playlist":
        raise ValueError("Please provide a single video URL")

    return info


def public_format(f):
    return {
        "format_id": f.get("format_id"),
        "ext": f.get("ext"),
        "height": f.get("height"),
        "width": f.get("width"),
        "fps": f.get("fps"),
        "filesize": f.get("filesize") or f.get("filesize_approx"),
        "vcodec": f.get("vcodec"),
        "acodec": f.get("acodec"),
        "has_video": f.get("vcodec") not in (None, "none"),
        "has_audio": f.get("acodec") not in (None, "none"),
        "protocol": f.get("protocol"),
    }


@app.route("/")
def home():
    return jsonify({
        "status": "ok",
        "service": "YouTube Media API",
        "endpoints": ["/info", "/download"]
    })


@app.route("/info", methods=["GET", "POST"])
def info():
    check_key()

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.args.get("url") or "").strip()

    if not url:
        return jsonify({"error": "Missing url"}), 400

    try:
        video = extract_video(url)
        formats = []

        for f in video.get("formats", []):
            if not f.get("format_id") or not f.get("url"):
                continue

            # Return formats that contain at least video or audio.
            if f.get("vcodec") == "none" and f.get("acodec") == "none":
                continue

            formats.append(public_format(f))

        return jsonify({
            "type": "video",
            "id": video.get("id"),
            "title": video.get("title"),
            "thumbnail": video.get("thumbnail"),
            "duration": video.get("duration"),
            "webpage_url": video.get("webpage_url"),
            "formats": formats,
        })

    except Exception as e:
        return jsonify({"error": str(e)[:400]}), 502


@app.route("/download", methods=["GET", "POST"])
def download():
    check_key()

    data = request.get_json(silent=True) or {}
    url = (data.get("url") or request.args.get("url") or "").strip()
    format_id = str(
        data.get("format_id") or request.args.get("format_id") or ""
    ).strip()

    if not url or not format_id:
        return jsonify({
            "error": "Provide url and format_id"
        }), 400

    try:
        video = extract_video(url)

        selected = next(
            (
                f for f in video.get("formats", [])
                if str(f.get("format_id")) == format_id
                and f.get("url")
            ),
            None,
        )

        if not selected:
            return jsonify({
                "error": "Format unavailable; call /info again"
            }), 404

        direct_url = selected["url"]

        return jsonify({
            "title": video.get("title"),
            "format": public_format(selected),
            "direct_url": direct_url,
            "http_headers": selected.get("http_headers", {}),
            "expires_note": (
                "The media URL may expire. Request a new URL through /download."
            ),
        })

    except Exception as e:
        return jsonify({"error": str(e)[:400]}), 502


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000))
    )
