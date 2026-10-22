import os
import shutil
import tempfile
from contextlib import contextmanager
from flask import Flask, request, jsonify, Response, stream_with_context
import yt_dlp

app = Flask(__name__)

SECRET_COOKIES_PATH = os.environ.get("COOKIES_FILE_PATH", "/etc/secrets/cookies.txt")


class LoggerCollector:
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
    runtimes = {}
    for rt in ("deno", "node", "bun", "qjs"):
        path = shutil.which(rt)
        if path:
            runtimes[rt] = {"path": path}
    if not runtimes:
        runtimes = {"node": {}, "deno": {}}
    return runtimes


def filter_real_media_formats(raw_formats):
    valid_formats = []
    for f in raw_formats or []:
        format_id = f.get("format_id")
        url = f.get("url")
        if not format_id or not url:
            continue

        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        ext = f.get("ext") or ""
        format_note = f.get("format_note") or ""

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
            "format_note": format_note,
            "is_missing_pot": "MISSING POT" in format_note.upper(),
            "url": url,
        })
    return valid_formats


def make_ydl_options(cookie_path=None, player_clients=None, include_missing_pot=False, logger=None):
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
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        # تقسيم التحميل إلى أجزاء 10MB لمنع يوتيوب من قطع الاتصال (يحل مشكلة unexpected end of stream)
        "http_chunk_size": 10485760,
        "js_runtimes": detect_js_runtimes(),
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
    attempts_log = []

    with temporary_cookie_file() as temp_cookie:
        strategies = []

        if temp_cookie:
            # المحاولة 1: العملاء الذين لا يطلبون PO Token مع الكوكيز (مثل web_safari و web_embedded و web)
            strategies.append({
                "name": "safari_and_web_with_cookies",
                "use_cookies": True,
                "clients": ["web_safari", "web", "mweb"],
                "include_missing_pot": False,
            })
            # المحاولة 2: إظهار جميع الصيغ بما فيها missing_pot كخيار احتياطي
            strategies.append({
                "name": "all_web_with_missing_pot",
                "use_cookies": True,
                "clients": ["mweb", "web", "web_creator"],
                "include_missing_pot": True,
            })

        strategies.append({
            "name": "cookieless_clients",
            "use_cookies": False,
            "clients": ["ios", "android_vr", "tv_simply"],
            "include_missing_pot": False,
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
                    "valid_media_formats_count": 0,
                    "exception": str(exc),
                    "warnings": logger.warnings,
                    "errors": logger.errors,
                })

        return None, [], attempts_log


def download_media_via_ytdlp(url, requested_format_id):
    """
    يقوم بتحميل الصيغة المطلوبة عبر محرك yt-dlp نفسه داخل مجلد مؤقت في /tmp
    مع تخطي صيغ MISSING POT المعطوبة تلقائياً، ثم يرجع مسار الملف المؤقت لبثه وحذفه.
    """
    temp_dir = tempfile.mkdtemp(prefix="yt_dl_", dir="/tmp")
    outtmpl = os.path.join(temp_dir, "media.%(ext)s")

    # تحديد محدد الصيغة بذكاء:
    # إذا طلب التطبيق صيغة صوت (مثل 140 أو audio)، نختار أفضل صوت يعمل بدون 403
    audio_ids = {"140", "139", "249", "250", "251", "599", "600", "233", "234", "audio"}
    is_audio_req = str(requested_format_id) in audio_ids or "audio" in str(requested_format_id).lower()

    if is_audio_req:
        # يجرب الصيغة المطلوبة أولاً، وإذا أعطت 403 يجرب أفضل صوت m4a/webm أو HLS متاح
        format_selector = f"{requested_format_id}/bestaudio[ext=m4a]/bestaudio/best"
    else:
        # للفيديو: يفضل صيغة تحتوي على صوت وصورة معاً (b) حتى يعمل الصوت في التطبيق، ثم الصيغة المطلوبة
        format_selector = f"b[ext=mp4]/{requested_format_id}/b/bestvideo[ext=mp4]/best"

    with temporary_cookie_file() as temp_cookie:
        # نجرب أولاً بعميل web_safari و web (لأنه يعطي صيغ HLS وصيغ مدمجة لا تطلب PO Token ولا تعطي 403)
        client_trials = [
            {"clients": ["web_safari", "ios", "mweb", "web"], "missing_pot": False},
            {"clients": ["mweb", "web"], "missing_pot": True},
        ]

        last_err = None
        for trial in client_trials:
            opts = make_ydl_options(
                cookie_path=temp_cookie,
                player_clients=trial["clients"],
                include_missing_pot=trial["missing_pot"]
            )
            opts["format"] = format_selector
            opts["outtmpl"] = outtmpl

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])

                # التأكد من وجود الملف المحمل في المجلد المؤقت
                files = [os.path.join(temp_dir, f) for f in os.listdir(temp_dir) if not f.endswith(".part")]
                if files and os.path.getsize(files[0]) > 0:
                    return temp_dir, files[0]
            except Exception as e:
                last_err = e
                # تنظيف أي ملفات جزئية قبل المحاولة التالية
                for f in os.listdir(temp_dir):
                    try:
                        os.remove(os.path.join(temp_dir, f))
                    except OSError:
                        pass

        shutil.rmtree(temp_dir, ignore_errors=True)
        raise Exception(f"Download failed on server: {str(last_err)}")


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
            "error": "No playable video or audio formats could be extracted.",
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

    # إذا كان الطلب بثاً مباشراً (كما يطلب تطبيق الأندرويد stream=true)
    if stream_mode:
        try:
            temp_dir, file_path = download_media_via_ytdlp(url, format_id)
        except Exception as e:
            return jsonify({
                "success": False,
                "error": str(e)
            }), 502

        file_size = os.path.getsize(file_path)
        ext = os.path.splitext(file_path)[1].lstrip(".") or "mp4"
        mime = "audio/mp4" if ext in ("m4a", "aac") else ("video/mp4" if ext == "mp4" else "application/octet-stream")

        def generate_and_cleanup():
            try:
                with open(file_path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        yield chunk
            finally:
                # حذف الملف المؤقت والمجلد فوراً بعد انتهاء الإرسال للتطبيق (صفر تخزين دائم)
                shutil.rmtree(temp_dir, ignore_errors=True)

        headers = {
            "Content-Type": mime,
            "Content-Length": str(file_size),
            "Content-Disposition": f'attachment; filename="download_{format_id}.{ext}"',
        }

        return Response(
            stream_with_context(generate_and_cleanup()),
            status=200,
            headers=headers
        )

    # الوضع الافتراضي (استجابة JSON بدون stream=true)
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
        return jsonify({
            "success": False,
            "error": f"Requested format_id '{format_id}' is not available.",
            "available_format_ids": [f["format_id"] for f in formats]
        }), 404

    direct_url = target_raw.get("url")
    http_headers = target_raw.get("http_headers") or video.get("http_headers") or {}
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
                "protocol": f["protocol"],
                "is_missing_pot": f["is_missing_pot"]
            }
            for f in formats[:15]
        ],
        "attempts_log": attempts_log
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
