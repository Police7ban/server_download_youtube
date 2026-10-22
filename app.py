import os
import shutil
import tempfile
import logging
from urllib.parse import urlparse

from flask import Flask, request, jsonify, abort
import yt_dlp

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

logging.basicConfig(level=logging.INFO)
app.logger.setLevel(logging.INFO)

API_KEY = os.environ.get("API_KEY", "")
COOKIES_FILE = "/etc/secrets/cookies.txt"


def check_key():
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        abort(401)


def get_request_data():
    data = request.get_json(silent=True) or {}
    return data


def valid_youtube_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()

        allowed_hosts = {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
            "youtu.be",
            "music.youtube.com",
        }

        return (
            parsed.scheme == "https"
            and host in allowed_hosts
        )
    except Exception:
        return False




def make_ydl_options():
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 2,
        "extractor_args": {
            "youtube": {
                "player_client": ["web"]
            }
        },
    }



def extract_video(url):
    if not valid_youtube_url(url):
        raise ValueError("Invalid YouTube URL")

    opts = make_ydl_options()
    temp_cookie = None

    try:
        if (
            os.path.isfile(COOKIES_FILE)
            and os.access(COOKIES_FILE, os.R_OK)
        ):
            # Render Secret Files are read-only.
            # Use a separate writable temporary copy.
            fd, temp_cookie = tempfile.mkstemp(
                prefix="ytcookies_",
                suffix=".txt",
                dir="/tmp",
            )
            os.close(fd)
            shutil.copyfile(COOKIES_FILE, temp_cookie)
            opts["cookiefile"] = temp_cookie

        with yt_dlp.YoutubeDL(opts) as ydl:
            video = ydl.extract_info(url, download=False)

        if not video:
            raise ValueError("No video information returned")

        if video.get("_type") in ("playlist", "multi_video"):
            raise ValueError("Please provide a single video URL")

        return video

    finally:
        if temp_cookie and os.path.exists(temp_cookie):
            try:
                os.remove(temp_cookie)
            except OSError:
                app.logger.warning(
                    "Could not remove temporary cookie file"
                )


def public_format(f):
    vcodec = f.get("vcodec")
    acodec = f.get("acodec")

    return {
        "format_id": f.get("format_id"),
        "format_note": f.get("format_note"),
        "ext": f.get("ext"),
        "height": f.get("height"),
        "width": f.get("width"),
        "fps": f.get("fps"),
        "filesize": f.get("filesize") or f.get("filesize_approx"),
        "vcodec": vcodec,
        "acodec": acodec,
        "has_video": vcodec not in (None, "none"),
        "has_audio": acodec not in (None, "none"),
        "protocol": f.get("protocol"),
    }


@app.route("/", methods=["GET"])
def home():
    return jsonify({
        "status": "ok",
        "service": "YouTube Media API",
        "version": 2,
        "endpoints": {
            "info": "/info",
            "download": "/download",
        },
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/info", methods=["GET", "POST"])
def info():
    check_key()

    data = get_request_data()
    url = (
        data.get("url")
        or request.args.get("url")
        or ""
    ).strip()

    if not url:
        return jsonify({"error": "Missing url"}), 400

    try:
        video = extract_video(url)
        formats = []

        for f in video.get("formats") or []:
            if not f.get("format_id") or not f.get("url"):
                continue

            vcodec = f.get("vcodec")
            acodec = f.get("acodec")

            if vcodec == "none" and acodec == "none":
                continue

            formats.append(public_format(f))

        return jsonify({
            "success": True,
            "type": "video",
            "id": video.get("id"),
            "title": video.get("title"),
            "thumbnail": video.get("thumbnail"),
            "duration": video.get("duration"),
            "webpage_url": video.get("webpage_url"),
            "uploader": video.get("uploader"),
            "view_count": video.get("view_count"),
            "formats_count": len(formats),
            "formats": formats,
        })

    except Exception as e:
        app.logger.exception("Video info extraction failed")
        return jsonify({
            "success": False,
            "error": str(e)[:500],
        }), 502


@app.route("/download", methods=["GET", "POST"])
def download():
    check_key()

    data = get_request_data()

    url = (
        data.get("url")
        or request.args.get("url")
        or ""
    ).strip()

    format_id = str(
        data.get("format_id")
        or request.args.get("format_id")
        or ""
    ).strip()

    if not url:
        return jsonify({"error": "Missing url"}), 400

    if not format_id:
        return jsonify({
            "error": "Missing format_id. Call /info first."
        }), 400

    try:
        video = extract_video(url)

        selected = next(
            (
                f for f in (video.get("formats") or [])
                if str(f.get("format_id")) == format_id
                and f.get("url")
            ),
            None,
        )

        if not selected:
            return jsonify({
                "success": False,
                "error": (
                    "Format not available. Call /info again "
                    "and choose a format_id from its response."
                ),
            }), 404

        return jsonify({
            "success": True,
            "title": video.get("title"),
            "format": public_format(selected),
            "direct_url": selected["url"],
            "http_headers": selected.get("http_headers") or {},
            "note": (
                "Direct media URLs may expire. "
                "Request a fresh URL when needed."
            ),
        })

    except Exception as e:
        app.logger.exception("Download URL extraction failed")
        return jsonify({
            "success": False,
            "error": str(e)[:500],
        }), 502


@app.errorhandler(401)
def unauthorized(error):
    return jsonify({
        "error": "Unauthorized. Check your X-API-Key header."
    }), 401


@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
    )
