import os
import shutil
import tempfile
import traceback
from contextlib import contextmanager
from flask import Flask, request, jsonify, Response, stream_with_context
import requests
import yt_dlp

app = Flask(__name__)

# المسار الافتراضي لملف الكوكيز في Render Secret Files
SECRET_COOKIES_PATH = os.environ.get("COOKIES_FILE_PATH", "/etc/secrets/cookies.txt")


class LoggerCollector:
    """التقاط رسائل وتحذيرات yt-dlp لتحليلها دون كشف أي بيانات حساسة."""
    def __init__(self):
        self.debugs = []
        self.warnings = []
        self.errors = []

    def debug(self, msg):
        # نحتفظ فقط بالرسائل المتعلقة باختيار العميل أو محرك JS
        if any(k in msg for k in ["Extracting URL", "Downloading", "player_client", "JS", " formats"]):
            self.debugs.append(msg)

    def warning(self, msg):
        self.warnings.append(msg)

    def error(self, msg):
        self.errors.append(msg)


@contextmanager
def temporary_cookie_file():
    """
    ينسخ ملف الكوكيز من /etc/secrets/cookies.txt (Read-only) إلى ملف مؤقت في /tmp
    ويضمن حذفه فوراً بعد انتهاء الطلب حتى لو حدث خطأ.
    """
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


def inspect_cookie_file_safely(path):
    """
    يفحص وجود وصحة هيكل ملف الكوكيز وأسماء الكوكيز الأساسية ليوتيوب
    دون قراءة أو طباعة أي قيم سرية نهائياً.
    """
    report = {
        "exists": False,
        "readable": False,
        "has_netscape_header": False,
        "total_cookie_lines": 0,
        "youtube_auth_keys_present": [],
    }
    if not path or not os.path.exists(path):
        return report

    report["exists"] = True
    important_keys = {
        "SID", "HSID", "SSID", "APISID", "SAPISID",
        "__Secure-1PSID", "__Secure-3PSID", "LOGIN_INFO", "__Secure-1PSIDTS"
    }
    found_keys = set()

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                stripped = line.strip()
                if i == 0 and ("Netscape" in stripped or "HTTP Cookie File" in stripped):
                    report["has_netscape_header"] = True
                if not stripped or stripped.startswith("#"):
                    continue
                parts = stripped.split("\t")
                if len(parts) >= 7:
                    report["total_cookie_lines"] += 1
                    cookie_name = parts[5].strip()
                    if cookie_name in important_keys:
                        found_keys.add(cookie_name)
        report["readable"] = True
        report["youtube_auth_keys_present"] = sorted(list(found_keys))
    except Exception as e:
        report["error"] = str(e)

    return report


def filter_real_media_formats(raw_formats):
    """
    تصفية الصيغ لاستبعاد صور الـ Storyboards (mhtml) والصيغ التي لا تملك رابطاً،
    مع الحفاظ على صيغ الصوت فقط (Audio only) والفيديو فقط (Video only) والصيغ المدمجة.
    """
    valid_formats = []
    for f in raw_formats or []:
        format_id = f.get("format_id")
        url = f.get("url")
        if not format_id or not url:
            continue

        vcodec = f.get("vcodec") or "none"
        acodec = f.get("acodec") or "none"
        ext = f.get("ext") or ""

        # استبعاد Storyboards والصور
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


def make_ydl_options(cookie_path=None, player_clients=None, logger=None):
    """
    بناء إعدادات yt-dlp بشكل نظيف.
    - لا نستخدم ignore_no_formats_error حتى لا نخفي الفشل.
    - لا نفرض format='best' حتى لا نمنع استخراج كافة الصيغ أو نفشل عند غياب صيغة مدمجة.
    """
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
        "extractor_retries": 2,
        # السماح بتحميل مكونات فك التشفير البرمجية (EJS) عند الحاجة
        "remote_components": ["ejs:github"],
    }

    if logger:
        opts["logger"] = logger

    if cookie_path:
        opts["cookiefile"] = cookie_path

    if player_clients:
        opts["extractor_args"] = {
            "youtube": {
                "player_client": player_clients
            }
        }

    return opts


def extract_video_with_fallback(url):
    """
    يستخدم استراتيجية متعدّدة المراحل (Multi-stage Fallback) للتغلب على قيود Render:
    1. المحاولة الأولى (إذا وجد ملف كوكيز): استخدام العملاء المتوافقين مع الكوكيز (web_creator, tv, mweb, web).
       ملاحظة: عميل web_creator و tv غالباً ما يتجاوزان قيود PO Token في حال كانت الكوكيز صالحة.
    2. المحاولة الثانية (بدون كوكيز): في حال كانت الكوكيز منتهية الصلاحية أو مقيدة، نجرب عملاء لا يستخدمون الكوكيز
       مثل (ios, android_vr, tv_simply).
    """
    attempts_log = []

    with temporary_cookie_file() as temp_cookie:
        strategies = []

        if temp_cookie:
            strategies.append({
                "name": "authenticated_clients_with_cookies",
                "use_cookies": True,
                # هؤلاء العملاء يدعمون الكوكيز رسمياً في yt-dlp ولا يتم تخطيهم
                "clients": ["tv", "web_creator", "mweb", "web"],
            })

        strategies.append({
            "name": "cookieless_embedded_and_mobile_clients",
            "use_cookies": False,
            # هؤلاء العملاء يعملون بدون كوكيز فقط
            "clients": ["ios", "android_vr", "tv_simply", "web_embedded"],
        })

        last_exception = None

        for strategy in strategies:
            logger = LoggerCollector()
            cookie_to_pass = temp_cookie if strategy["use_cookies"] else None
            opts = make_ydl_options(
                cookie_path=cookie_to_pass,
                player_clients=strategy["clients"],
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
                last_exception = exc
                attempts_log.append({
                    "strategy": strategy["name"],
                    "clients_requested": strategy["clients"],
                    "used_cookies": strategy["use_cookies"],
                    "valid_media_formats_count": 0,
                    "exception": str(exc),
                    "warnings": logger.warnings,
                    "errors": logger.errors,
                })

        # إذا وصلنا إلى هنا، فهذا يعني أن جميع الاستراتيجيات لم ترجع أي صيغ فيديو/صوت حقيقية
        error_msg = "Failed to extract any playable audio/video formats across all strategies."
        if last_exception:
            error_msg = f"{error_msg} Last error: {str(last_exception)}"
        return None, [], attempts_log


def get_request_param(key, default=None):
    """قراءة الباراميتر سواء أُرسل عبر Query String (GET) أو JSON Body أو Form (POST)."""
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
        "endpoints": ["/health", "/info", "/download", "/diagnose"]
    }), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "healthy",
        "success": True,
        "yt_dlp_version": yt_dlp.version.__version__,
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

    # رفض إرجاع HTTP 200 مضلل إذا كانت الصيغ فارغة
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

    # البحث عن الصيغة المطلوبة داخل الصيغ الخام للحصول على http_headers الخاصة بها
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

    # إذا طلب العميل البث المباشر عبر السيرفر لتجنب حظر الـ IP (403 Forbidden) دون تخزين الملف
    if stream_mode:
        req_headers = dict(http_headers)
        # تمرير ترويسة Range إذا كان مشغل الفيديو لدى العميل يطلب تقديم/تأخير الفيديو
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

    # الوضع الافتراضي: إرجاع JSON يحتوي على الرابط المباشر والترويسات ورابط البث البديل
    proxy_stream_url = f"{request.host_url.rstrip('/')}/download?url={url}&format_id={format_id}&stream=true"

    return jsonify({
        "success": True,
        "id": video.get("id"),
        "title": video.get("title"),
        "format_id": str(format_id),
        "ext": target_clean.get("ext"),
        "resolution": target_clean.get("resolution"),
        "has_video": target_clean.get("has_video"),
        "has_audio": target_clean.get("has_audio"),
        "filesize": target_clean.get("filesize"),
        "direct_url": direct_url,
        "http_headers": http_headers,
        "proxy_stream_url": proxy_stream_url,
        "note": "If direct_url returns HTTP 403 on the client due to IP binding, use proxy_stream_url."
    }), 200


@app.route("/diagnose", methods=["GET", "POST"])
def diagnose():
    """
    اختبار تشخيصي آمن يفحص كل عميل على حدة، وجود JS Runtime، وصلاحية هيكل الكوكيز
    دون كشف أي محتوى من ملف الكوكيز.
    """
    url = get_request_param("url", "https://www.youtube.com/watch?v=Pk1PBxK49_Y")

    # 1. فحص وجود محركات JavaScript في بيئة Render
    js_runtimes = {
        "node": shutil.which("node"),
        "deno": shutil.which("deno"),
        "bun": shutil.which("bun"),
        "qjs": shutil.which("qjs"),
    }

    # 2. فحص آمن لهيكل ملف الكوكيز
    cookie_inspection = inspect_cookie_file_safely(SECRET_COOKIES_PATH)

    # 3. اختبار العملاء منفردين لمعرفة سلوك كل عميل بدقة
    test_matrix = [
        {"label": "tv_with_cookies", "clients": ["tv"], "cookies": True},
        {"label": "web_creator_with_cookies", "clients": ["web_creator"], "cookies": True},
        {"label": "mweb_with_cookies", "clients": ["mweb"], "cookies": True},
        {"label": "ios_without_cookies", "clients": ["ios"], "cookies": False},
        {"label": "android_vr_without_cookies", "clients": ["android_vr"], "cookies": False},
        {"label": "tv_simply_without_cookies", "clients": ["tv_simply"], "cookies": False},
    ]

    matrix_results = []
    with temporary_cookie_file() as temp_cookie:
        for case in test_matrix:
            if case["cookies"] and not temp_cookie:
                matrix_results.append({
                    "test": case["label"],
                    "skipped": "Cookie file not found at /etc/secrets/cookies.txt"
                })
                continue

            logger = LoggerCollector()
            opts = make_ydl_options(
                cookie_path=temp_cookie if case["cookies"] else None,
                player_clients=case["clients"],
                logger=logger
            )
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info_dict = ydl.extract_info(url, download=False)
                    raw_f = info_dict.get("formats") or []
                    valid_f = filter_real_media_formats(raw_f)
                    matrix_results.append({
                        "test": case["label"],
                        "client": case["clients"],
                        "used_cookies": case["cookies"],
                        "raw_formats_total": len(raw_f),
                        "playable_formats_count": len(valid_f),
                        "sample_format_ids": [f["format_id"] for f in valid_f[:8]],
                        "warnings": logger.warnings,
                        "errors": logger.errors,
                    })
            except Exception as e:
                matrix_results.append({
                    "test": case["label"],
                    "client": case["clients"],
                    "used_cookies": case["cookies"],
                    "playable_formats_count": 0,
                    "exception": str(e),
                    "warnings": logger.warnings,
                    "errors": logger.errors,
                })

    return jsonify({
        "yt_dlp_version": yt_dlp.version.__version__,
        "js_runtimes_detected": js_runtimes,
        "cookie_file_check": cookie_inspection,
        "client_tests": matrix_results
    }), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
