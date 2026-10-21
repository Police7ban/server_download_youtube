import os
import requests
from flask import Flask, request, jsonify, Response, abort
import yt_dlp

app = Flask(__name__)
API_KEY = os.environ.get("API_KEY", "")  # اختياري: ضيفه في Environment على Render


def check_key():
    if API_KEY and request.headers.get("X-API-Key") != API_KEY:
        abort(401)


def base_opts():
    o = {"quiet": True, "no_warnings": True, "skip_download": True}
    if os.path.exists("cookies.txt"):
        o["cookiefile"] = "cookies.txt"
    return o


def best_thumb(entry, vid):
    thumbs = entry.get("thumbnails") or []
    if thumbs and thumbs[-1].get("url"):
        return thumbs[-1]["url"]
    if entry.get("thumbnail"):
        return entry["thumbnail"]
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"


@app.route("/")
def home():
    return "OK"


@app.route("/info", methods=["POST"])
def info():
    check_key()
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url:
        return jsonify({"error": "missing url"}), 400

    opts = base_opts()
    opts["extract_flat"] = "in_playlist"
    opts["noplaylist"] = False

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(url, download=False)

        items = []
        if res.get("_type") == "playlist":
            for e in res.get("entries") or []:
                if not e:
                    continue
                vid = e.get("id")
                if not vid:
                    continue
                items.append({
                    "id": vid,
                    "title": e.get("title") or "Untitled",
                    "thumbnail": best_thumb(e, vid),
                    "duration": e.get("duration") or 0,
                    "url": f"https://www.youtube.com/watch?v={vid}",
                })
            return jsonify({
                "type": "playlist",
                "title": res.get("title") or "Playlist",
                "count": len(items),
                "items": items,
            })

        vid = res.get("id")
        items.append({
            "id": vid,
            "title": res.get("title") or "Untitled",
            "thumbnail": best_thumb(res, vid),
            "duration": res.get("duration") or 0,
            "url": res.get("webpage_url") or url,
        })
        return jsonify({"type": "video", "title": res.get("title"), "count": 1, "items": items})
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


@app.route("/stream")
def stream():
    check_key()
    url = request.args.get("url", "").strip()
    mode = request.args.get("mode", "audio")  # audio | video
    key = request.args.get("key", "")
    if API_KEY and key != API_KEY and request.headers.get("X-API-Key") != API_KEY:
        abort(401)
    if not url:
        return jsonify({"error": "missing url"}), 400

    opts = base_opts()
    opts["noplaylist"] = True
    if mode == "video":
        opts["format"] = "best[ext=mp4][height<=720]/best[ext=mp4]/best"
    else:
        opts["format"] = "bestaudio[ext=m4a]/bestaudio/best"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            res = ydl.extract_info(url, download=False)
        direct = res["url"]
        headers = dict(res.get("http_headers") or {})
        if request.headers.get("Range"):
            headers["Range"] = request.headers["Range"]

        r = requests.get(direct, headers=headers, stream=True, timeout=30)
        resp = Response(r.iter_content(chunk_size=64 * 1024), status=r.status_code)
        for h in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if h in r.headers:
                resp.headers[h] = r.headers[h]
        return resp
    except Exception as e:
        return jsonify({"error": str(e)[:300]}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
