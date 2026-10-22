import os
import shutil
import tempfile
from contextlib import contextmanager
from flask import Flask, request, jsonify, Response, stream_with_context
import requests
import yt_dlp

app = Flask(__name__)

SECRET_COOKIES_PATH = os.environ.get("COOKIES_FILE_PATH", "/etc/secrets/cookies.txt")


class LoggerCollector:
    """التقاط رسائل وتحذيرات yt-dlp لتحليلها دون كشف أي بيانات حساسة."""
    def __init__(self):
        self.warnings = []
        self.errors = []

    def debug(self, msg):
        pass

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.errors.append(msg)


@contextmanager
def temporary_cookie_file():
    """نسخ ملف الكوكيز من /etc/secrets/cookies.txt إلى /tmp وحذفه فور انتهاء الطلب."""
    if not os.path.exists(SECRET_COOKIES_PATH):
        yield None
        return

    fd, temp_path = tempfile.mkstemp(prefix="yt_cookies_", suffix=".txt", dir="/tmp")
    os.close(fd)
    try:
        shutil.copyfile(SECRET_COOKIES_PATH, temp_path)
        yield temp_path
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def detect_js_runtimes():
    """
    يكتشف محركات JavaScript المثبتة على سيرفر Render (مثل Node.js أو Deno)
    ويجهزها بالصيغة التي يتطلبها yt-dlp 2026 لحل تحدي n-challenge.
    """
    runtimes = {}
    for rt in ("deno", "node", "bun", "qjs"):
        path = shutil.which(rt)
        if path:
            runtimes[rt] = {"path": path}
    # إذا لم يعثر shutil.which، نفعل node و deno افتراضياً
    if not runtimes:
        runtimes = {"node": {}, "deno": {}}
    return runtimes


def filter_real_media_formats(raw_formats):
    """تصفية الصيغ لاستبعاد Storyboards والاحتفاظ بجميع صيغ الفيديو والصوت (HTTPS + HLS)."""
    valid_formats = []
    for f in raw_formats or []:
        format_id = f.get("format_id")
        url = f.get("url")
        if not format_id or not url:
            continue

        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        ext = f.get("ext") or ""

        if (vcodec == "none" and acodec == "none") or ext == "mhtml" or str(format_id).startswith("sb"):
            continue

        has_video = vcodec != "none"
        has_audio = acodec != "none"

        valid_formats.append({
            "format_id": str(format_id),
            "ext": ext,
            "resolution": f.get("resolution") or (f"{f.get('width')}x{f.get('height')}" if f.get("width") else "audio only"),
            "width": f.get("width"),
            "height": f.get("height"),
            "fps": f.get("fps"),
            "vcodec": vcodec,
            "acodec": acodec,
            "has_video": has_video,
            "has_audio": has_audio,
            "is_merged": has_video and has_audio,
            "filesize": f.get("filesize") or f.get("filesize_approx"),
            "tbr": f.get("tbr"),
            "protocol": f.get("protocol"),
            "format_note": f.get("format_note"),
            "url": url,
        })
    return valid_formats


def make_ydl_options(cookie_path=None, player_clients=None, include_missing_pot=True, logger=None):
    """
    إعداد yt-dlp مع:
    1. تفعيل js_runtimes (Node.js / Deno) لحل n-challenge.
    2. تفعيل formats=missing_pot لعدم إسقاط صيغ HTTPS مثل 140 و 137 على Render.
    """
    youtube_args = {}
    if player_clients:
        youtube_args["player_client"] = player_clients
    if include_missing_pot:
        youtube_args["formats"] = ["missing_pot"]

    opts = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 2,
        # تفعيل محركات JS المثبتة على السيرفر (Node مثبت مسبقاً على Render)
        "js_runtimes": detect_js_runtimes(),
        # السماح بتحميل سكربتات التحدي كخيار احتياطي إضافي بجانب حزمة yt-dlp-ejs
        "remote_components": ["ejs:github", "ejs:npm"],
        "extractor_args": {
            "youtube": youtube_args
        }
    }

    if logger:
        opts["logger"] = logger

    if cookie_path:
        opts["cookiefile"] = cookie_path

    return opts


def extract_video_with_fallback(url):
    """
    استراتيجية الاستخراج المتدرجة:
    1. web + mweb مع الكوكيز وتفعيل Node JS Runtime والسماح بصيغ missing_pot (الأعلى نجاحاً).
    2. web_creator + mweb مع الكوكيز.
    3. بدون كوكيز في حال تعطل الكوكيز مستقبلاً.
    """
    attempts_log = []

    with temporary_cookie_file() as temp_cookie:
        strategies = []

        if temp_cookie:
            # الاستراتيجية 1: عملاء الويب الرسميون للكوكيز (بدون عميل tv الذي يسبب خطأ Reload)
            strategies.append({
                "name": "web_and_mweb_with_cookies_and_missing_pot",
                "use_cookies": True,
                "clients": ["mweb", "web"],
                "include_missing_pot": True,
            })
            # الاستراتيجية 2: عميل منشئي المحتوى منفرداً
            strategies.append({
                "name": "web_creator_with_cookies",
                "use_cookies": True,
                "clients": ["web_creator"],
                "include_missing_pot": True,
            })

        # الاستراتيجية 3: بدون كوكيز كخط دفاع أخير
        strategies.append({
            "name": "cookieless_clients",
            "use_cookies": False,
            "clients": ["ios", "android_vr", "tv_simply"],
            "include_missing_pot": True,
        })

        for strategy in strategies:
            logger = LoggerCollector()
            cookie_to_pass = temp_cookie if strategy["use_cookies"] else None
            opts = make_ydl_options(
                cookie_path=cookie_to_pass,
                player_clients=strategy["clients"],
                include_missing_pot=strategy["include_missing_pot"],
                logger=logger
            )

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    video_info = ydl.extract_info(url, download=False)
                    raw_formats = video_info.get("formats") or []
                    clean_formats = filter_real_media_formats(raw_formats)

                    attempts_log.append({
                        "strategy": strategy["name"],
                        "clients_requested": strategy["clients"],
                        "used_cookies": strategy["use_cookies"],
                        "js_runtimes_passed": list(opts["js_runtimes"].keys()),
                        "raw_formats_count": len(raw_formats),
                        "valid_media_formats_count": len(clean_formats),
                        "warnings": logger.warnings,
                        "errors": logger.errors,
                    })

                    if len(clean_formats) > 0:
                        return video_info, clean_formats, attempts_log

            except Exception as exc:
                attempts_log.append({
                    "strategy": strategy["name"],
                    "clients_requested": strategy["clients"],
                    "used_cookies": strategy["use_cookies"],
                    "js_runtimes_passed": list(opts["js_runtimes"].keys()),
                    "valid_media_formats_count": 0,
                    "exception": str(exc),
                    "warnings": logger.warnings,
                    "errors": logger.errors,
                })

        return None, [], attempts_log


def get_request_param(key, default=None):
    if request.is_json:
        data = request.get_json(silent=True) or {}
        if key in data:
            return data[key]
    if key in request.values:
        return request.values[key]
    return default


@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "success": True,
        "service": "YouTube Extraction API on Render",
        "yt_dlp_version": yt_dlp.version.__version__,
        "js_runtimes_detected": detect_js_runtimes(),
        "endpoints": ["/health", "/info", "/download", "/diagnose"]
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "success": True,
        "yt_dlp_version": yt_dlp.version.__version__,
        "js_runtimes_detected": detect_js_runtimes(),
        "cookies_configured": os.path.exists(SECRET_COOKIES_PATH)
    }), 200


@app.route("/info", methods=["GET", "POST"])
def info():
    url = get_request_param("url")
    if not url:
        return jsonify({
            "success": False,
            "error": "Parameter 'url' is required."
        }), 400

    video, formats, attempts_log = extract_video_with_fallback(url)

    if not video or len(formats) == 0:
        return jsonify({
            "success": False,
            "error": "No playable video or audio formats could be extracted. Check diagnostics field for exact cause.",
            "formats": [],
            "formats_count": 0,
            "diagnostics": attempts_log
        }), 502

    return jsonify({
        "success": True,
        "id": video.get("id"),
        "title": video.get("title"),
        "thumbnail": video.get("thumbnail"),
        "duration": video.get("duration"),
        "webpage_url": video.get("webpage_url") or url,
        "uploader": video.get("uploader"),
        "view_count": video.get("view_count"),
        "type": video.get("_type", "video"),
        "formats_count": len(formats),
        "formats": formats
    }), 200


@app.route("/download", methods=["GET", "POST"])
def download():
    url = get_request_param("url")
    format_id = get_request_param("format_id")
    stream_mode = str(get_request_param("stream", "false")).lower() in ("true", "1", "yes")

    if not url or not format_id:
        return jsonify({
            "success": False,
            "error": "Both 'url' and 'format_id' parameters are required."
        }), 400

    video, formats, attempts_log = extract_video_with_fallback(url)

    if not video or len(formats) == 0:
        return jsonify({
            "success": False,
            "error": "Could not extract media formats for this video.",
            "diagnostics": attempts_log
        }), 502

    raw_formats = video.get("formats") or []
    target_raw = next((f for f in raw_formats if str(f.get("format_id")) == str(format_id)), None)
    target_clean = next((f for f in formats if str(f.get("format_id")) == str(format_id)), None)

    if not target_raw or not target_clean:
        available_ids = [f["format_id"] for f in formats]
        return jsonify({
            "success": False,
            "error": f"Requested format_id '{format_id}' is not available.",
            "available_format_ids": available_ids
        }), 404

    direct_url = target_raw.get("url")
    http_headers = target_raw.get("http_headers") or video.get("http_headers") or {}

    if stream_mode:
        req_headers = dict(http_headers)
        if "Range" in request.headers:
            req_headers["Range"] = request.headers["Range"]

        upstream = requests.get(direct_url, headers=req_headers, stream=True, timeout=30)
        response_headers = {
            "Content-Type": upstream.headers.get("Content-Type", "application/octet-stream"),
            "Content-Disposition": f'attachment; filename="{video.get("id")}_{format_id}.{target_clean.get("ext", "mp4")}"',
        }
        if "Content-Length" in upstream.headers:
            response_headers["Content-Length"] = upstream.headers["Content-Length"]
        if "Content-Range" in upstream.headers:
            response_headers["Content-Range"] = upstream.headers["Content-Range"]
            response_headers["Accept-Ranges"] = "bytes"

        return Response(
            stream_with_context(upstream.iter_content(chunk_size=65536)),
            status=upstream.status_code,
            headers=response_headers
        )

    proxy_stream_url = f"{request.host_url.rstrip('/')}/download?url={url}&format_id={format_id}&stream=true"

    return jsonify({
        "success": True,
        "id": video.get("id"),
        "title": video.get("title"),
        "format_id": str(format_id),
        "ext": target_clean.get("ext"),
        "resolution": target_clean.get("resolution"),
        "protocol": target_clean.get("protocol"),
        "has_video": target_clean.get("has_video"),
        "has_audio": target_clean.get("has_audio"),
        "filesize": target_clean.get("filesize"),
        "direct_url": direct_url,
        "http_headers": http_headers,
        "proxy_stream_url": proxy_stream_url
    }), 200


@app.route("/diagnose", methods=["GET", "POST"])
def diagnose():
    url = get_request_param("url", "https://www.youtube.com/watch?v=Pk1PBxK49_Y")
    video, formats, attempts_log = extract_video_with_fallback(url)
    return jsonify({
        "yt_dlp_version": yt_dlp.version.__version__,
        "js_runtimes_detected": detect_js_runtimes(),
        "success": len(formats) > 0,
        "formats_extracted_count": len(formats),
        "sample_formats": [
            {
                "format_id": f["format_id"],
                "ext": f["ext"],
                "resolution": f["resolution"],
                "protocol": f["protocol"]
            }
            for f in formats[:15]
        ],
        "attempts_log": attempts_log
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
