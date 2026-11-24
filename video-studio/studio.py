"""Video Studio personal MVP. Python 3.11+, standard library only."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import hmac
import http.cookies
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("studio")
TERMINAL = {"done", "failed", "preview"}
LABELS = {"queued": "Đang chờ", "writing": "Đang viết", "review": "Chờ duyệt", "ready": "Chờ dựng", "submitting": "Đang gửi", "rendering": "Đang dựng", "done": "Hoàn tất", "failed": "Có lỗi", "attention": "Cần kiểm tra", "preview": "Xem thử hoàn tất"}


class Config:
    def __init__(self, **overrides):
        self.mode = os.getenv("STUDIO_MODE", "demo")
        self.host = os.getenv("STUDIO_HOST", "127.0.0.1")
        self.port = int(os.getenv("STUDIO_PORT", "8765"))
        self.data = Path(os.getenv("STUDIO_DATA", str(ROOT / "data")))
        self.password = os.getenv("STUDIO_PASSWORD", "")
        self.internal_key = os.getenv("STUDIO_INTERNAL_KEY", "")
        self.secure_cookie = os.getenv("STUDIO_SECURE_COOKIE", "false").lower() == "true"
        self.llm_url = os.getenv("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        self.llm_key = os.getenv("LLM_API_KEY", "")
        self.llm_model = os.getenv("LLM_MODEL", "")
        self.mpt_url = os.getenv("MPT_BASE_URL", "http://moneyprinter:8080").rstrip("/")
        self.mpt_key = os.getenv("MPT_API_KEY", "")
        self.materials = json.loads(os.getenv("MPT_LOCAL_MATERIALS", "[]"))
        self.daily_limit = int(os.getenv("DAILY_VIDEO_LIMIT", "3"))
        self.reserve_bytes = int(os.getenv("MIN_FREE_MB", "2048")) * 1024**2
        self.max_video_bytes = int(os.getenv("MAX_VIDEO_MB", "512")) * 1024**2
        self.retention_days = int(os.getenv("VIDEO_RETENTION_DAYS", "30"))
        self.__dict__.update(overrides)
        self.data = Path(self.data)
        if not isinstance(self.materials, list) or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+\.(png|jpg|mp4)", name) for name in self.materials):
            raise ValueError("MPT_LOCAL_MATERIALS must be a JSON list of safe material filenames")
        if self.mode not in {"demo", "live"}:
            raise ValueError("STUDIO_MODE must be demo or live")
        if self.host not in {"127.0.0.1", "localhost", "::1"} and (len(self.password) < 12 or len(self.internal_key) < 32):
            raise ValueError("Network access requires a password of 12+ characters and an internal key of 32+ characters")


class Store:
    def __init__(self, folder):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "studio.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                    state TEXT NOT NULL, payload TEXT NOT NULL, progress INTEGER DEFAULT 0,
                    task_id TEXT DEFAULT '', error TEXT DEFAULT '', output TEXT DEFAULT '',
                    kept INTEGER DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL,
                    finished REAL DEFAULT 0, submitted REAL DEFAULT 0, source_cleaned INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["label"] = LABELS[item["state"]]
        return item

    def get(self, job_id):
        with self.connect() as db:
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def all(self):
        with self.connect() as db:
            return [self.decode(r) for r in db.execute("SELECT * FROM jobs ORDER BY created DESC")]

    def update(self, job_id, **fields):
        allowed = {"state", "payload", "progress", "task_id", "error", "output", "kept", "finished", "submitted", "source_cleaned"}
        if not fields.keys() <= allowed:
            raise ValueError("Unknown fields")
        if "payload" in fields:
            fields["payload"] = json.dumps(fields["payload"], ensure_ascii=False)
        fields["updated"] = time.time()
        with self.lock, self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(f"{name}=?" for name in fields) + " WHERE id=?", (*fields.values(), job_id))

    def create(self, payload, request_key):
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,100}", request_key):
            raise ValueError("Mã yêu cầu không hợp lệ. Hãy tải lại trang.")
        topic = str(payload.get("topic", "")).strip()
        if not 3 <= len(topic) <= 500:
            raise ValueError("Chủ đề cần từ 3 đến 500 ký tự.")
        script = str(payload.get("script", "")).strip()
        if len(script) > 8000:
            raise ValueError("Kịch bản tối đa 8.000 ký tự.")
        seconds = int(payload.get("seconds", 45))
        if seconds not in {30, 45, 60}:
            raise ValueError("Chọn thời lượng 30, 45 hoặc 60 giây.")
        voice = payload.get("voice", "female")
        if voice not in {"female", "male"}:
            raise ValueError("Giọng đọc không hợp lệ.")
        clean = {"topic": topic, "script": script, "seconds": seconds, "voice": voice,
                 "style": str(payload.get("style", "Dễ hiểu, gần gũi"))[:300],
                 "review": payload.get("review", True) is not False, "terms": []}
        with self.lock, self.connect() as db:
            existing = db.execute("SELECT id FROM jobs WHERE request_key=?", (request_key,)).fetchone()
            if existing:
                return self.get(existing["id"])
            if db.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN ('done','failed','preview')").fetchone()[0] >= 20:
                raise ValueError("Hàng đợi đã có 20 công việc. Hãy xử lý bớt trước.")
            job_id = secrets.token_hex(12)
            now = time.time()
            db.execute("INSERT INTO jobs(id,request_key,state,payload,created,updated) VALUES(?,?,'queued',?,?,?)",
                       (job_id, request_key, json.dumps(clean, ensure_ascii=False), now, now))
        return self.get(job_id)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Services:
    def __init__(self, cfg):
        self.cfg = cfg
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, url, method="GET", body=None, key="", header="x-api-key", timeout=60):
        headers = {"Accept": "application/json"}
        if key:
            headers[header] = "Bearer " + key if header == "Authorization" else key
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=timeout) as response:
            raw = response.read(2 * 1024**2 + 1)
            if len(raw) > 2 * 1024**2:
                raise ValueError("Phản hồi dịch vụ vượt giới hạn.")
            return json.loads(raw) if raw else {}

    def write(self, payload):
        if payload["script"]:
            return payload["script"], [payload["topic"]]
        if not self.cfg.llm_key or not self.cfg.llm_model:
            raise ValueError("Chưa cấu hình dịch vụ AI. Có thể nhập kịch bản có sẵn để bỏ qua bước này.")
        prompt = (f"Viết lời thoại tiếng Việt cho video khoảng {payload['seconds']} giây. "
                  f"Chủ đề: {payload['topic']}. Phong cách: {payload['style']}. "
                  "Không bịa số liệu, không thêm hướng dẫn sân khấu. Trả đúng JSON với script là chuỗi lời thoại "
                  "và terms là mảng 3-5 từ khóa tiếng Anh ngắn để tìm video tư liệu. Không markdown.")
        response = self.request(self.cfg.llm_url + "/chat/completions", "POST", {
            "model": self.cfg.llm_model, "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1000, "temperature": 0.6,
        }, self.cfg.llm_key, "Authorization")
        content = response["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        result = json.loads(content)
        if not isinstance(result.get("script"), str) or not 10 <= len(result["script"]) <= 8000:
            raise ValueError("AI trả về kịch bản không hợp lệ.")
        terms = result.get("terms", [])
        if not isinstance(terms, list) or not terms or any(not isinstance(t, str) or len(t) > 100 for t in terms):
            raise ValueError("AI trả về từ khóa tư liệu không hợp lệ.")
        return result["script"], terms[:5]

    def submit(self, payload):
        voice = "vi-VN-HoaiMyNeural-Female" if payload["voice"] == "female" else "vi-VN-NamMinhNeural-Male"
        body = {
            "video_subject": payload["topic"], "video_script": payload["script"],
            "video_terms": payload["terms"] or [payload["topic"]], "video_language": "vi-VN",
            "video_aspect": "9:16", "video_count": 1, "video_source": "pexels",
            "voice_name": voice, "subtitle_enabled": True, "font_name": "BeVietnamPro-Bold.ttf",
            "subtitle_animation": "none", "video_transition_mode": None,
            "bgm_type": "random", "bgm_volume": 0.1, "n_threads": 2,
        }
        if self.cfg.materials:
            body.update(video_source="local", image_motion="static", bgm_type="none", bgm_volume=0,
                        video_materials=[{"provider": "local", "url": name, "duration": 6} for name in self.cfg.materials])
        result = self.request(self.cfg.mpt_url + "/api/v1/videos", "POST", body, self.cfg.mpt_key)
        task_id = result.get("data", {}).get("task_id", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
            raise ValueError("Dịch vụ không trả về mã video hợp lệ.")
        return task_id

    def status(self, task_id):
        return self.request(self.cfg.mpt_url + "/api/v1/tasks/" + task_id, key=self.cfg.mpt_key)["data"]

    def download(self, task_id, url, target):
        parsed = urllib.parse.urlsplit(url)
        base = urllib.parse.urlsplit(self.cfg.mpt_url)
        if parsed.netloc and (parsed.scheme, parsed.netloc) != (base.scheme, base.netloc):
            raise ValueError("Đường dẫn video ngoài dịch vụ đã cấu hình.")
        path = urllib.parse.unquote(parsed.path)
        if not path.startswith(f"/tasks/{task_id}/") or ".." in path.split("/") or "\\" in path or not path.endswith(".mp4"):
            raise ValueError("Đường dẫn thành phẩm không hợp lệ.")
        full_url = self.cfg.mpt_url + parsed.path
        headers = {"x-api-key": self.cfg.mpt_key}
        temp = target.with_suffix(".part")
        total = 0
        try:
            with self.opener.open(urllib.request.Request(full_url, headers=headers), timeout=90) as response, temp.open("wb") as output:
                expected = int(response.headers.get("Content-Length", "0"))
                if expected > self.cfg.max_video_bytes:
                    raise ValueError("Video vượt giới hạn dung lượng.")
                while chunk := response.read(256 * 1024):
                    total += len(chunk)
                    if total > self.cfg.max_video_bytes or shutil.disk_usage(target.parent).free < self.cfg.reserve_bytes:
                        raise ValueError("Không đủ dung lượng để lưu thành phẩm.")
                    output.write(chunk)
            if total < 12 or (expected and total != expected):
                raise ValueError("Tệp video tải chưa đầy đủ.")
            with temp.open("rb") as stream:
                if stream.read(12)[4:8] != b"ftyp":
                    raise ValueError("Thành phẩm không phải tệp MP4 hợp lệ.")
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

    def delete_source(self, task_id):
        self.request(self.cfg.mpt_url + "/api/v1/tasks/" + task_id, "DELETE", key=self.cfg.mpt_key)


class Studio:
    def __init__(self, cfg, services=None):
        self.cfg = cfg
        self.store = Store(cfg.data)
        self.services = services or Services(cfg)
        self.videos = cfg.data / "videos"
        self.videos.mkdir(exist_ok=True)
        self.engine_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.last_cleanup = 0
        self.login_attempts = {}
        # A process may have died after sending a paid request. Never replay it.
        for job in self.store.all():
            if job["state"] in {"writing", "submitting"}:
                self.store.update(job["id"], state="attention", error="Lần xử lý trước bị gián đoạn. Cần kiểm tra trước khi gửi lại để tránh tính phí trùng.")

    def check_capacity(self):
        if shutil.disk_usage(self.cfg.data).free < self.cfg.reserve_bytes + self.cfg.max_video_bytes:
            raise ValueError("Dung lượng máy chủ thấp. Hãy dọn thư viện trước khi tạo video.")

    def tick(self):
        if not self.engine_lock.acquire(blocking=False):
            return {"busy": True}
        try:
            if self.cfg.mode == "live" and time.time() - self.last_cleanup > 3600:
                self.cleanup()
                self.last_cleanup = time.time()
            jobs = list(reversed(self.store.all()))
            # Uncertain submissions reserve the render slot until reconciled.
            blockers = [j for j in jobs if j["state"] == "attention" and not j["task_id"] and j["submitted"]]
            if blockers:
                return {"blocked": True}
            active = next((j for j in jobs if j["state"] in {"rendering", "attention"} and j["task_id"]), None)
            job = active or next((j for j in jobs if j["state"] in {"queued", "ready"}), None)
            if not job:
                return {"idle": True}
            job_id, state, payload = job["id"], job["state"], job["payload"]
            try:
                if state == "queued":
                    self.store.update(job_id, state="writing", progress=10, error="")
                    if self.cfg.mode == "demo":
                        script = payload["script"] or (f"Đây là bản xem thử về chủ đề: {payload['topic']}. "
                            "Mở đầu bằng một tình huống gần gũi. Tiếp theo trình bày ba ý chính, mỗi ý đi kèm một ví dụ dễ hiểu. "
                            "Kết thúc bằng một gợi ý hành động nhỏ. Nội dung này được tạo bằng mẫu minh họa, chưa gọi AI.")
                        terms = ["everyday life", "learning", "ideas"]
                    else:
                        script, terms = self.services.write(payload)
                    payload.update(script=script, terms=terms)
                    self.store.update(job_id, payload=payload, state="review" if payload["review"] else "ready", progress=25)
                elif state == "ready":
                    if self.cfg.mode == "demo":
                        self.store.update(job_id, state="preview", progress=100, finished=time.time())
                    else:
                        self.check_capacity()
                        midnight = time.time() - 86400
                        count = sum(j["submitted"] >= midnight for j in jobs)
                        if count >= self.cfg.daily_limit:
                            return {"waiting_for_daily_limit": True}
                        self.store.update(job_id, state="submitting", progress=30, submitted=time.time())
                        task_id = self.services.submit(payload)
                        self.store.update(job_id, state="rendering", task_id=task_id, progress=35)
                elif state in {"rendering", "attention"}:
                    status = self.services.status(job["task_id"])
                    if status.get("state") == -1:
                        self.store.update(job_id, state="failed", error="MoneyPrinterTurbo báo lỗi. Kiểm tra cấu hình giọng đọc, tư liệu và nhật ký máy chủ.", finished=time.time())
                    elif status.get("state") == 1:
                        videos = status.get("videos") or status.get("combined_videos") or []
                        if not videos:
                            raise ValueError("Tác vụ báo xong nhưng chưa có video.")
                        target = self.videos / f"{job_id}.mp4"
                        self.services.download(job["task_id"], videos[0], target)
                        self.store.update(job_id, state="done", output=target.name, progress=100, error="", finished=time.time())
                    else:
                        self.store.update(job_id, state="rendering", error="", progress=max(35, min(94, int(status.get("progress", 0)))))
                return {"job_id": job_id, "state": self.store.get(job_id)["state"]}
            except Exception as error:
                current = self.store.get(job_id)
                # HTTP errors, malformed responses and lost connections after POST are all ambiguous.
                ambiguous = current["state"] in {"writing", "submitting", "rendering", "attention"}
                message = "Dịch vụ tạm thời chưa phản hồi. " if isinstance(error, (urllib.error.URLError, TimeoutError, OSError)) else "Không thể hoàn tất bước xử lý. "
                if isinstance(error, ValueError):
                    message = str(error)
                if ambiguous:
                    message += " Công việc được giữ lại; không tự gửi yêu cầu tính phí lần nữa."
                self.store.update(job_id, state="attention" if ambiguous else "failed", error=message[:700])
                LOG.warning("Job %s stopped at %s (%s)", job_id, current["state"], type(error).__name__)
                return {"job_id": job_id, "state": self.store.get(job_id)["state"]}
        finally:
            self.engine_lock.release()

    def cleanup(self):
        now = time.time()
        for job in self.store.all():
            if job["state"] != "done" or not job["finished"]:
                continue
            if job["output"] and not job["source_cleaned"] and now - job["finished"] > 86400:
                if (self.videos / job["output"]).is_file():
                    try:
                        self.services.delete_source(job["task_id"])
                        self.store.update(job["id"], source_cleaned=1)
                    except Exception:
                        LOG.warning("Source cleanup deferred for %s", job["id"])
            if not job["kept"] and job["output"] and now - job["finished"] > self.cfg.retention_days * 86400:
                # Keep our only copy until source cleanup succeeded; never delete active tasks.
                if self.store.get(job["id"])["source_cleaned"]:
                    (self.videos / job["output"]).unlink(missing_ok=True)
                    self.store.update(job["id"], output="", error="Thành phẩm đã hết thời gian lưu; kịch bản vẫn còn.")

    def worker(self):
        while not self.stop_event.is_set():
            self.tick()
            self.stop_event.wait(2)

    def public_job(self, job):
        return {k: v for k, v in job.items() if k not in {"request_key", "output", "source_cleaned"}} | {"has_video": bool(job["output"])}

    def action(self, job_id, action, body):
        with self.engine_lock:
            job = self.store.get(job_id)
            if not job:
                raise ValueError("Không tìm thấy công việc.")
            if action == "approve":
                if job["state"] != "review":
                    raise ValueError("Công việc không còn ở bước duyệt.")
                script = str(body.get("script", job["payload"]["script"])).strip()
                if not 10 <= len(script) <= 8000:
                    raise ValueError("Kịch bản cần từ 10 đến 8.000 ký tự.")
                payload = job["payload"] | {"script": script}
                self.store.update(job_id, payload=payload, state="ready")
            elif action == "keep":
                self.store.update(job_id, kept=int(body.get("kept") is True))
            elif action == "delete_video":
                if job["state"] != "done" or body.get("confirm") is not True:
                    raise ValueError("Chỉ xóa thành phẩm đã hoàn tất khi có xác nhận.")
                if job["task_id"] and not job["source_cleaned"]:
                    self.services.delete_source(job["task_id"])
                if job["output"] == f"{job_id}.mp4":
                    (self.videos / job["output"]).unlink(missing_ok=True)
                self.store.update(job_id, output="", kept=0, source_cleaned=1, error="Bạn đã xóa video. Kịch bản được giữ lại.")
            elif action == "attach":
                if job["state"] != "attention" or job["task_id"] or not job["submitted"]:
                    raise ValueError("Chỉ gắn mã cho yêu cầu dựng đang cần kiểm tra.")
                task_id = str(body.get("task_id", ""))
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                    raise ValueError("Mã tác vụ không hợp lệ.")
                status = self.services.status(task_id)
                if not isinstance(status, dict):
                    raise ValueError("Không tìm thấy tác vụ.")
                self.store.update(job_id, task_id=task_id, state="rendering", error="")
            elif action == "retry_write":
                if job["state"] != "attention" or job["submitted"] or job["task_id"] or body.get("confirm") is not True:
                    raise ValueError("Cần xác nhận viết lại; có thể phát sinh phí AI.")
                self.store.update(job_id, state="queued", error="")
            else:
                raise ValueError("Thao tác không được hỗ trợ.")
        return self.public_job(self.store.get(job_id))


def handler_for(studio):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VideoStudio/0.1"

        def log_message(self, fmt, *args):
            # Do not log URLs, credentials, prompts, session cookies or request bodies.
            pass

        def headers_common(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; media-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

        def reply(self, status, data, content_type="application/json; charset=utf-8", extra=None):
            raw = json.dumps(data, ensure_ascii=False).encode() if not isinstance(data, bytes) else data
            self.send_response(status)
            self.headers_common()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(raw)

        def authenticated(self):
            if not studio.cfg.password:
                return True
            cookie = http.cookies.SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie", ""))
                token = cookie["studio_session"].value
            except (KeyError, http.cookies.CookieError):
                return False
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            with studio.store.connect() as db:
                return db.execute("SELECT 1 FROM sessions WHERE token_hash=? AND expires>?", (token_hash, time.time())).fetchone() is not None

        def origin_allowed(self):
            origin = self.headers.get("Origin")
            if origin and urllib.parse.urlsplit(origin).netloc != self.headers.get("Host"):
                return False
            return self.headers.get("X-Studio-Request") == "1"

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if path == "/healthz":
                return self.reply(200, {"ok": True})
            if path in {"/", "/app.js", "/style.css"}:
                name = "index.html" if path == "/" else path[1:]
                content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8", "style.css": "text/css; charset=utf-8"}[name]
                return self.reply(200, (ROOT / "web" / name).read_bytes(), content_type)
            if not self.authenticated():
                return self.reply(401, {"error": "Vui lòng đăng nhập."})
            if path == "/api/status":
                disk = shutil.disk_usage(studio.cfg.data)
                return self.reply(200, {"mode": studio.cfg.mode, "daily_limit": studio.cfg.daily_limit,
                    "free_gb": round(disk.free / 1024**3, 1), "retention_days": studio.cfg.retention_days,
                    "llm_ready": bool(studio.cfg.llm_key and studio.cfg.llm_model), "mpt_ready": bool(studio.cfg.mpt_key),
                    "requires_login": bool(studio.cfg.password)})
            if path == "/api/jobs":
                return self.reply(200, [studio.public_job(j) for j in studio.store.all()])
            match = re.fullmatch(r"/api/jobs/([a-f0-9]{24})/video", path)
            if match:
                return self.serve_video(match[1])
            return self.reply(404, {"error": "Không tìm thấy."})

        def serve_video(self, job_id):
            job = studio.store.get(job_id)
            if not job or job["state"] != "done" or job["output"] != f"{job_id}.mp4":
                return self.reply(404, {"error": "Chưa có video để phát."})
            path = studio.videos / job["output"]
            if not path.is_file():
                return self.reply(404, {"error": "Thành phẩm đã hết hạn lưu."})
            size = path.stat().st_size
            start, end = 0, size - 1
            range_header = self.headers.get("Range")
            if range_header:
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
                if not match or (not match[1] and not match[2]):
                    return self.reply(416, {"error": "Khoảng dữ liệu không hợp lệ."}, extra={"Content-Range": f"bytes */{size}"})
                if match[1]:
                    start = int(match[1]); end = min(int(match[2]), size - 1) if match[2] else size - 1
                else:
                    suffix = int(match[2]); start = max(0, size - suffix)
                    if suffix == 0:
                        start = size
                if start >= size or start > end:
                    return self.reply(416, {"error": "Khoảng dữ liệu không hợp lệ."}, extra={"Content-Range": f"bytes */{size}"})
            self.send_response(206 if range_header else 200)
            self.headers_common()
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if query.get("download") == ["1"]:
                self.send_header("Content-Disposition", f'attachment; filename="video-{job_id}.mp4"')
            self.end_headers()
            try:
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        chunk = stream.read(min(256 * 1024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 32768:
                    return self.reply(413, {"error": "Yêu cầu quá lớn."})
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("Dữ liệu không hợp lệ.")
                if self.path == "/internal/tick":
                    supplied = self.headers.get("X-Studio-Key", "")
                    if not studio.cfg.internal_key or not hmac.compare_digest(supplied, studio.cfg.internal_key):
                        return self.reply(403, {"error": "Forbidden"})
                    return self.reply(200, studio.tick())
                if not self.origin_allowed():
                    return self.reply(403, {"error": "Yêu cầu không hợp lệ. Hãy mở trực tiếp trang ứng dụng."})
                if self.path == "/api/login":
                    return self.login(body)
                if not self.authenticated():
                    return self.reply(401, {"error": "Vui lòng đăng nhập."})
                if self.path == "/api/stop-preview":
                    if studio.cfg.mode != "demo":
                        return self.reply(403, {"error": "Chỉ dừng bản xem thử, không dừng máy chủ thật."})
                    self.reply(200, {"ok": True})
                    studio.stop_event.set()
                    threading.Thread(target=self.server.shutdown, daemon=True).start()
                    return
                if self.path == "/api/logout":
                    cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
                    if "studio_session" in cookie:
                        with studio.store.connect() as db:
                            db.execute("DELETE FROM sessions WHERE token_hash=?", (hashlib.sha256(cookie["studio_session"].value.encode()).hexdigest(),))
                    return self.reply(200, {"ok": True}, extra={"Set-Cookie": "studio_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})
                if self.path == "/api/jobs":
                    return self.reply(201, studio.public_job(studio.store.create(body, self.headers.get("Idempotency-Key", ""))))
                match = re.fullmatch(r"/api/jobs/([a-f0-9]{24})/(approve|keep|delete_video|attach|retry_write)", self.path)
                if match:
                    return self.reply(200, studio.action(match[1], match[2], body))
                return self.reply(404, {"error": "Không tìm thấy."})
            except (TypeError, json.JSONDecodeError):
                return self.reply(400, {"error": "Dữ liệu chưa hợp lệ hoặc thao tác không còn áp dụng. Kiểm tra nội dung và tải lại danh sách."})
            except ValueError as error:
                return self.reply(400, {"error": str(error)[:400]})
            except Exception as error:
                LOG.warning("Request failed (%s)", type(error).__name__)
                return self.reply(500, {"error": "Chưa xử lý được yêu cầu. Vui lòng thử lại sau."})

        def login(self, body):
            ip = self.client_address[0]
            recent = [t for t in studio.login_attempts.get(ip, []) if time.time() - t < 300]
            studio.login_attempts[ip] = recent
            if len(recent) >= 10:
                return self.reply(429, {"error": "Bạn thử quá nhiều lần. Vui lòng chờ 5 phút."})
            password = body.get("password", "")
            if not isinstance(password, str) or not hmac.compare_digest(password.encode(), studio.cfg.password.encode()):
                recent.append(time.time())
                return self.reply(401, {"error": "Mật khẩu chưa đúng."})
            token = secrets.token_urlsafe(32)
            with studio.store.connect() as db:
                db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
                db.execute("INSERT INTO sessions VALUES (?,?)", (hashlib.sha256(token.encode()).hexdigest(), time.time() + 43200))
            cookie = f"studio_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=43200"
            if studio.cfg.secure_cookie:
                cookie += "; Secure"
            return self.reply(200, {"ok": True}, extra={"Set-Cookie": cookie})

    return Handler


def main():
    logging.basicConfig(level=logging.WARNING)
    cfg = Config()
    studio = Studio(cfg)
    if cfg.mode == "demo":
        threading.Thread(target=studio.worker, daemon=True).start()
    server = ThreadingHTTPServer((cfg.host, cfg.port), handler_for(studio))
    server.daemon_threads = True
    server.timeout = 30
    print(f"Video Studio [{cfg.mode}] http://{cfg.host}:{cfg.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        studio.stop_event.set()
        server.server_close()


if __name__ == "__main__":
    main()
