#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
曲风大闯关 · 薄后端（BFF）
--------------------------------
职责：持有 app_key、计算 X-QYOPI-Sign 签名、代调 QQ 音乐 OpenAPI，
      把清洗后的歌曲元数据返回给前端；同时托管前端页面（同源，免 CORS）。

运行：
    export QQ_APP_ID=你的appid
    export QQ_APP_KEY=你的appkey
    # 可选：配置用户登录态才能拿到播放 url（VIP），只做元数据可不配
    # export QQ_OPEN_ID=... ; export QQ_ACCESS_TOKEN=...
    python3 server.py           # 默认 http://127.0.0.1:8000

没有配置 QQ_APP_ID / QQ_APP_KEY 时，/api/song 会返回内置 mock 数据，
demo 依然可跑（路演不怕断网/没 key）。
"""
import os, re, time, json, hmac, hashlib, base64, binascii
import urllib.parse, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
HOST = os.environ.get("HOST", "127.0.0.1")   # 对公网暴露时设为 0.0.0.0
PORT = int(os.environ.get("PORT", "8000"))

APP_ID       = os.environ.get("QQ_APP_ID", "").strip()
APP_KEY      = os.environ.get("QQ_APP_KEY", "").strip()
# 可选用户登录态（拿播放 url 用；纯元数据可留空）
OPEN_APPID   = os.environ.get("QQ_OPEN_APPID", APP_ID).strip()
OPEN_ID      = os.environ.get("QQ_OPEN_ID", "").strip()
ACCESS_TOKEN = os.environ.get("QQ_ACCESS_TOKEN", "").strip()

GATEWAY = os.environ.get(
    "QQ_GATEWAY",
    "https://openrpc.music.qq.com/rpc_proxy/fcgi-bin/music_open_api.fcg",
)

# 内置回退数据（未配置 key 时使用）
MOCK_SONG = {
    "source": "mock",
    "song_name": "稻香",
    "singer_name": "周杰伦",
    "album_name": "魔杰座",
    "album_pic": "",          # mock 无图，前端自动回退到内置视觉
    "song_mid": "0039MnYb0qxYhV",
    "playable": 0,
    "song_play_url": "",
    "note": "未配置 QQ_APP_ID / QQ_APP_KEY，返回内置数据",
}

MOCK_SINGER = {
    "source": "mock",
    "singer_name": "周杰伦",
    "singer_mid": "0025NhlN2yWrP4",
    "singer_pic": "",
    "album_num": 0,
    "song_num": 0,
    "note": "未配置 QQ_APP_ID / QQ_APP_KEY，返回内置数据",
}

MOCK_LYRIC = {
    "source": "mock",
    "song_name": "青花瓷",
    "singer_name": "周杰伦",
    "lines": [
        "素胚勾勒出青花", "笔锋浓转淡", "瓶身描绘的牡丹", "一如你初妆",
        "冉冉檀香透过窗", "心事我了然", "宣纸上走笔至此搁一半",
        "釉色渲染仕女图", "韵味被私藏", "天青色等烟雨", "而我在等你",
    ],
    "note": "未配置 QQ_APP_ID / QQ_APP_KEY，返回内置歌词",
}
# 兜底歌词按每句 4 秒铺时间轴，与前端离线 fallback 的节奏一致
MOCK_LYRIC["timed"] = [{"t": i * 4.0, "s": s} for i, s in enumerate(MOCK_LYRIC["lines"])]


def qyopi_sign(query_str: str, app_key: str, cookie: str = "") -> str:
    """QQ 音乐主网关签名：HMAC-SHA256(参数串 + '&cookie=' + cookie, key=app_key) -> hex 小写。
    注意：参数不排序；用于签名的字符串必须与实际发出的完全一致。"""
    plain = query_str + "&cookie=" + cookie
    return hmac.new(app_key.encode("utf-8"), plain.encode("utf-8"),
                    hashlib.sha256).hexdigest().lower()


def _find_songs(obj):
    """递归找出返回体里所有像歌曲的对象（对网关是否包一层做兼容）。"""
    found = []
    def walk(o):
        if isinstance(o, dict):
            if "song_name" in o or "song_mid" in o:
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return found


def _clean(song: dict) -> dict:
    """挑出前端需要的字段。"""
    g = song.get
    return {
        "source": "qqmusic",
        "song_name": g("song_name") or g("song_title") or "",
        "singer_name": g("singer_name") or "",
        "album_name": g("album_name") or "",
        "genre": g("genre") or "",          # 流派 / 曲风
        "album_pic": g("album_pic_300x300") or g("album_pic_500x500")
                     or g("album_pic_150x150") or g("album_pic") or "",
        "song_mid": g("song_mid") or "",
        "song_h5_url": g("song_h5_url") or "",
        "playable": g("playable", 0),
        # 播放 url 需 VIP 登录态；未登录时为空，属预期
        "song_play_url": g("song_play_url") or g("try_30s_url") or "",
        "user_own_rule": song.get("user_own_rule", 0),
    }


def search_song(name: str) -> dict:
    if not APP_ID or not APP_KEY:
        return dict(MOCK_SONG)

    params = [
        ("opi_cmd", "fcg_music_custom_search.fcg"),
        ("app_id", APP_ID),
        ("timestamp", str(int(time.time()))),
        ("w", name),
        ("t", "0"),
        ("num", "5"),
    ]
    # 若配置了用户登录态则带上（用于拿播放 url）
    if OPEN_ID and ACCESS_TOKEN:
        params += [
            ("login_type", "6"),
            ("qqmusic_open_appid", OPEN_APPID),
            ("qqmusic_open_id", OPEN_ID),
            ("qqmusic_access_token", ACCESS_TOKEN),
        ]

    query = urllib.parse.urlencode(params)      # 签名与发送同一字符串
    sign = qyopi_sign(query, APP_KEY)
    url = GATEWAY + "?" + query
    req = urllib.request.Request(url, headers={"X-QYOPI-Sign": sign})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode("utf-8", "replace")
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return dict(MOCK_SONG, source="mock", note="调用失败，回退内置数据：%s" % e)

    songs = _find_songs(data)
    if not songs:
        return dict(MOCK_SONG, source="mock",
                    note="未搜到结果或返回结构不同，回退内置数据",
                    raw_ret=data.get("ret"))
    return _clean(songs[0])


def _find_singers(obj):
    """递归找出返回体里的歌手对象（含 singer_mid 且 album_num，用以区别于歌曲对象）。"""
    found = []
    def walk(o):
        if isinstance(o, dict):
            if "singer_mid" in o and ("album_num" in o or "song_num" in o):
                found.append(o)
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return found


def search_singer(query: str) -> dict:
    if not APP_ID or not APP_KEY:
        return dict(MOCK_SINGER)

    params = [
        ("opi_cmd", "fcg_music_custom_query_singer_list.fcg"),
        ("app_id", APP_ID),
        ("timestamp", str(int(time.time()))),
        ("query", query),
        ("page", "1"),
        ("page_size", "10"),
    ]
    if OPEN_ID and ACCESS_TOKEN:
        params += [
            ("login_type", "6"),
            ("qqmusic_open_appid", OPEN_APPID),
            ("qqmusic_open_id", OPEN_ID),
            ("qqmusic_access_token", ACCESS_TOKEN),
        ]

    q = urllib.parse.urlencode(params)
    sign = qyopi_sign(q, APP_KEY)
    req = urllib.request.Request(GATEWAY + "?" + q, headers={"X-QYOPI-Sign": sign})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return dict(MOCK_SINGER, source="mock", note="调用失败，回退内置数据：%s" % e)

    singers = _find_singers(data)
    if not singers:
        return dict(MOCK_SINGER, source="mock",
                    note="未搜到歌手或返回结构不同", raw_ret=data.get("ret"))
    s = singers[0]
    return {
        "source": "qqmusic",
        "singer_name": s.get("singer_name") or "",
        "singer_mid": s.get("singer_mid") or "",
        "singer_pic": s.get("singer_pic") or "",
        "album_num": s.get("album_num", 0),
        "song_num": s.get("song_num", 0),
    }


def _find_lyric(obj):
    """递归找出返回体里的 song_lyric 字段（对网关是否包一层做兼容）。"""
    if isinstance(obj, dict):
        if obj.get("song_lyric"):
            return obj["song_lyric"]
        for v in obj.values():
            r = _find_lyric(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_lyric(v)
            if r:
                return r
    return ""


_LRC_META = re.compile(r"^(作?词|作?曲|编曲|监制|制作|演唱|混音|录音)\s*[:：]")


_LRC_TIME = re.compile(r"\[(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?\]")


def _parse_lrc(text: str):
    """LRC → (纯歌词行列表, 带时间戳行列表)。
    timed 项形如 {"t": 29.54, "s": "素胚勾勒出青花笔锋浓转淡"}，按 t 升序；
    一行多时间标签（副歌复用）会展开成多项。"""
    lines, timed = [], []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        tags = _LRC_TIME.findall(raw)
        # 去掉所有 [mm:ss.xx] / [ti:...] 标签
        s = re.sub(r"\[[^\]]*\]", "", raw).strip()
        if not s or _LRC_META.match(s):
            continue
        if not lines and " - " in s:      # 首行常为「歌名 - 歌手」
            continue
        lines.append(s)
        for m, sec, frac in tags:
            t = int(m) * 60 + int(sec) + (int(frac) / 10 ** len(frac) if frac else 0.0)
            timed.append({"t": round(t, 2), "s": s})
    timed.sort(key=lambda x: x["t"])
    return lines, timed


def get_lyric(name: str = "", song_mid: str = "", song_id: str = "") -> dict:
    """歌词接口：fcg_music_custom_get_lyric.fcg（song_id 与 song_mid 必传其一）。
    只给歌名时先走搜索拿 song_mid，再取歌词。"""
    if not APP_ID or not APP_KEY:
        return dict(MOCK_LYRIC)

    song_name = ""
    if not song_mid and not song_id:
        song = search_song(name or "青花瓷")
        song_mid = song.get("song_mid") or ""
        song_name = song.get("song_name") or ""
        if not song_mid:
            return dict(MOCK_LYRIC, source="mock", note="搜索未拿到 song_mid，回退内置歌词")

    params = [
        ("opi_cmd", "fcg_music_custom_get_lyric.fcg"),
        ("app_id", APP_ID),
        ("timestamp", str(int(time.time()))),
    ]
    params.append(("song_mid", song_mid) if song_mid else ("song_id", song_id))

    query = urllib.parse.urlencode(params)
    sign = qyopi_sign(query, APP_KEY)
    req = urllib.request.Request(GATEWAY + "?" + query, headers={"X-QYOPI-Sign": sign})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return dict(MOCK_LYRIC, source="mock", note="调用失败，回退内置歌词：%s" % e)

    lyric = _find_lyric(data)
    if not lyric:
        return dict(MOCK_LYRIC, source="mock",
                    note="未拿到 song_lyric（ret=%s msg=%s），回退内置歌词"
                         % (data.get("ret"), data.get("msg")),
                    raw_ret=data.get("ret"))
    # 部分环境 song_lyric 为 base64；不含 LRC 标签时尝试解码
    if "[" not in lyric:
        try:
            lyric = base64.b64decode(lyric).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            pass

    lines, timed = _parse_lrc(lyric)
    if not lines:
        return dict(MOCK_LYRIC, source="mock", note="歌词解析为空，回退内置歌词")
    return {
        "source": "qqmusic",
        "song_name": song_name,
        "song_mid": song_mid,
        "lines": lines,
        "timed": timed,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == "/api/song":
            q = urllib.parse.parse_qs(u.query)
            name = (q.get("name") or ["稻香"])[0]
            # 兼容：终端直接传中文时会被按 latin-1 解码，这里还原为 UTF-8
            try:
                name = name.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass  # 前端已正确 encodeURIComponent 的情况保持原样
            try:
                self._send(200, json.dumps(search_song(name), ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps(dict(MOCK_SONG, source="mock",
                           note="服务异常，回退：%s" % e), ensure_ascii=False))
            return

        if u.path == "/api/singer":
            q = urllib.parse.parse_qs(u.query)
            name = (q.get("name") or ["周杰伦"])[0]
            try:
                name = name.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            try:
                self._send(200, json.dumps(search_singer(name), ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps(dict(MOCK_SINGER, source="mock",
                           note="服务异常，回退：%s" % e), ensure_ascii=False))
            return

        if u.path == "/api/lyric":
            q = urllib.parse.parse_qs(u.query)
            name = (q.get("name") or ["青花瓷"])[0]
            try:
                name = name.encode("latin-1").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            song_mid = (q.get("song_mid") or [""])[0]
            song_id = (q.get("song_id") or [""])[0]
            try:
                self._send(200, json.dumps(
                    get_lyric(name=name, song_mid=song_mid, song_id=song_id),
                    ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps(dict(MOCK_LYRIC, source="mock",
                           note="服务异常，回退：%s" % e), ensure_ascii=False))
            return

        # 静态文件（前端页面）
        path = u.path
        if path in ("/", ""):
            path = "/index.html"
        fp = os.path.normpath(os.path.join(HERE, path.lstrip("/")))
        if not fp.startswith(HERE) or not os.path.isfile(fp):
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        ext = os.path.splitext(fp)[1].lower()
        ALLOWED = {
            ".html": "text/html; charset=utf-8",
            ".js":   "application/javascript; charset=utf-8",
            ".css":  "text/css; charset=utf-8",
            ".mp3":  "audio/mpeg",
            ".jpg":  "image/jpeg", ".jpeg": "image/jpeg",
            ".png":  "image/png",
            ".svg":  "image/svg+xml",
        }
        if ext not in ALLOWED:          # 只下发白名单类型，避免泄露 server.py / README 等
            self._send(404, "not found", "text/plain; charset=utf-8")
            return
        size = os.path.getsize(fp)
        last_mod = self.date_time_string(int(os.path.getmtime(fp)))
        # html 每次回源校验，避免拿到旧页面；其余资源允许缓存一天，靠 URL 版本号更新
        cache_ctl = "no-cache" if ext == ".html" else "max-age=86400"
        rng = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        # If-Range 校验：文件已变更时忽略 Range 回整文件，防止新旧文件字节拼成坏图
        if_range = self.headers.get("If-Range")
        if if_range and if_range != last_mod:
            rng = None
        with open(fp, "rb") as f:
            if rng and (rng.group(1) or rng.group(2)):
                # Range 支持：音频 seek（bgm.currentTime）与 iOS Safari 播放都依赖 206
                if rng.group(1):
                    start = int(rng.group(1))
                    end = min(int(rng.group(2)) if rng.group(2) else size - 1, size - 1)
                else:                              # bytes=-N：取末尾 N 字节
                    start = max(0, size - int(rng.group(2)))
                    end = size - 1
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    self.end_headers()
                    return
                f.seek(start)
                body = f.read(end - start + 1)
                self.send_response(206)
                self.send_header("Content-Type", ALLOWED[ext])
                self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Last-Modified", last_mod)
                self.send_header("Cache-Control", cache_ctl)
                self.end_headers()
                self.wfile.write(body)
            else:
                body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", ALLOWED[ext])
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Last-Modified", last_mod)
                self.send_header("Cache-Control", cache_ctl)
                self.end_headers()
                self.wfile.write(body)

    def log_message(self, *a):
        pass  # 静默日志


if __name__ == "__main__":
    mode = "真实接口" if (APP_ID and APP_KEY) else "MOCK（未配置 key）"
    print("曲风大闯关薄后端启动：http://%s:%d  [%s]" % (HOST, PORT, mode))
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
