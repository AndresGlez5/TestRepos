#!/usr/bin/env python3
"""Dependency-free HTTP frontend and job runner for local audio downloads."""

from __future__ import annotations

import json
import importlib.util
import mimetypes
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse


APP_ROOT = Path(__file__).resolve().parent
STATIC_ROOT = APP_ROOT / "static"
DOWNLOAD_ROOT = Path(os.getenv("AUDIO_DOWNLOAD_ROOT", APP_ROOT / "downloads")).resolve()
STATE_ROOT = Path(os.getenv("AUDIO_STATE_ROOT", APP_ROOT / "data")).resolve()
ARCHIVE_ROOT = Path(os.getenv("AUDIO_ARCHIVE_ROOT", APP_ROOT / "archives")).resolve()
JOBS_FILE = STATE_ROOT / "jobs.json"
MAX_LOG_LINES = max(20, int(os.getenv("AUDIO_MAX_LOG_LINES", "160")))
MAX_REQUEST_BYTES = 64 * 1024
APP_PASSWORD = os.getenv("AUDIO_PASSWORD", "")
SECURE_COOKIES = os.getenv("AUDIO_SECURE_COOKIES", "1") == "1"

QUALITY_OPTIONS = {"128", "192", "256", "320"}
YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}
SPOTIFY_HOSTS = {"open.spotify.com"}

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.RLock()
WORK_QUEUE: queue.Queue[str] = queue.Queue()
SESSIONS: set[str] = set()
SESSIONS_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_url(raw_url: str) -> tuple[str, str]:
    """Validate and normalize a supported public Spotify or YouTube URL."""
    candidate = raw_url.strip()
    if not candidate or len(candidate) > 2048:
        raise ValueError("Enter a Spotify or YouTube link.")

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password:
        raise ValueError("Use a full https:// Spotify or YouTube link.")

    if host in YOUTUBE_HOSTS:
        if host == "youtu.be":
            if not parsed.path.strip("/"):
                raise ValueError("This YouTube link is missing a video ID.")
        elif parsed.path not in {"/watch", "/playlist"} and not parsed.path.startswith(
            ("/shorts/", "/live/")
        ):
            raise ValueError("Use a YouTube video or playlist link.")
        return "youtube", candidate

    if host in SPOTIFY_HOSTS:
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) < 2 or parts[0] not in {"track", "album", "playlist"}:
            raise ValueError("Use a Spotify track, album, or playlist link.")
        return "spotify", candidate

    raise ValueError("Only open.spotify.com, youtube.com, and youtu.be links are supported.")


def safe_job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("Invalid job ID")
    target = (DOWNLOAD_ROOT / job_id).resolve()
    target.relative_to(DOWNLOAD_ROOT)
    return target


def persist_jobs() -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    temporary = JOBS_FILE.with_suffix(".tmp")
    snapshot = list(JOBS.values())
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(JOBS_FILE)


def load_jobs() -> None:
    if not JOBS_FILE.exists():
        return
    try:
        items = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    with JOBS_LOCK:
        for job in items:
            if not isinstance(job, dict) or "id" not in job:
                continue
            if job.get("status") in {"queued", "running"}:
                job["status"] = "failed"
                job["message"] = "Interrupted by a server restart. Add the link again to retry."
                job["finished_at"] = utc_now()
            JOBS[job["id"]] = job
        persist_jobs()


def downloader_available(source: str) -> bool:
    module = "yt_dlp" if source == "youtube" else "spotdl"
    return importlib.util.find_spec(module) is not None


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in job.items()
        if key not in {"command", "output_dir"}
    }


def update_job(job_id: str, **changes: Any) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job.update(changes)
        persist_jobs()


def append_log(job_id: str, line: str) -> None:
    clean = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).strip()
    if not clean:
        return
    with JOBS_LOCK:
        log = JOBS[job_id].setdefault("log", [])
        log.append(clean[:600])
        del log[:-MAX_LOG_LINES]
        persist_jobs()


def yt_dlp_command(job: dict[str, Any], output_dir: Path) -> list[str]:
    output_template = str(output_dir / "%(playlist_index)03d - %(title)s.%(ext)s")
    return [
        sys.executable,
        "-m",
        "yt_dlp",
        "--yes-playlist",
        "--ignore-errors",
        "--continue",
        "--no-overwrites",
        "--windows-filenames",
        "--extract-audio",
        "--audio-format",
        "mp3",
        "--audio-quality",
        f"{job['quality']}K",
        "--embed-metadata",
        "--embed-thumbnail",
        "--newline",
        "--progress-template",
        "download:%(progress._percent_str)s",
        "--output",
        output_template,
        job["url"],
    ]


def spotdl_command(job: dict[str, Any], output_dir: Path) -> list[str]:
    output_template = str(
        output_dir / "{list-position} - {artists} - {title}.{output-ext}"
    )
    return [
        sys.executable,
        "-m",
        "spotdl",
        "download",
        job["url"],
        "--format",
        "mp3",
        "--bitrate",
        f"{job['quality']}k",
        "--threads",
        "4",
        "--output",
        output_template,
    ]


def scan_audio_files(job_id: str) -> list[dict[str, Any]]:
    root = safe_job_dir(job_id)
    if not root.exists():
        return []
    results = []
    for path in sorted(root.rglob("*.mp3"), key=lambda item: item.name.casefold()):
        relative = path.relative_to(root).as_posix()
        results.append(
            {
                "name": relative,
                "size": path.stat().st_size,
                "url": f"/api/jobs/{job_id}/files/{quote(relative, safe='')}",
            }
        )
    return results


def run_job(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        job = dict(job)

    binary = "yt-dlp" if job["source"] == "youtube" else "spotDL"
    if not downloader_available(job["source"]):
        update_job(
            job_id,
            status="failed",
            message=f"{binary} is not installed. Run this app with Docker or install its requirements.",
            finished_at=utc_now(),
        )
        return

    output_dir = safe_job_dir(job_id)
    output_dir.mkdir(parents=True, exist_ok=True)
    command = (
        yt_dlp_command(job, output_dir)
        if job["source"] == "youtube"
        else spotdl_command(job, output_dir)
    )

    update_job(
        job_id,
        status="running",
        progress=1,
        message="Fetching playlist details…",
        started_at=utc_now(),
    )

    try:
        process = subprocess.Popen(
            command,
            cwd=APP_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_log(job_id, line)
            match = re.search(r"download:\s*([0-9]+(?:\.[0-9]+)?)%", line)
            if match:
                progress = min(99, max(1, int(float(match.group(1)))))
                update_job(job_id, progress=progress, message="Downloading and converting…")
            elif "Downloaded" in line or "Converting" in line:
                update_job(job_id, message="Converting to MP3…")
        return_code = process.wait()
    except OSError as exc:
        update_job(
            job_id,
            status="failed",
            message=f"Could not start {binary}: {exc}",
            finished_at=utc_now(),
        )
        return

    files = scan_audio_files(job_id)
    if files:
        update_job(
            job_id,
            status="complete",
            progress=100,
            files=files,
            message=f"Ready — {len(files)} MP3 file{'s' if len(files) != 1 else ''}.",
            finished_at=utc_now(),
        )
    else:
        message = "No MP3 files were created. Check the job log for source restrictions or unavailable items."
        if return_code:
            message = f"The downloader exited with code {return_code}. " + message
        update_job(
            job_id,
            status="failed",
            progress=0,
            files=[],
            message=message,
            finished_at=utc_now(),
        )


def worker_loop() -> None:
    while True:
        job_id = WORK_QUEUE.get()
        try:
            run_job(job_id)
        finally:
            WORK_QUEUE.task_done()


def content_disposition(filename: str) -> str:
    ascii_name = re.sub(r"[^A-Za-z0-9._ -]", "_", filename).strip() or "download.mp3"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class AppHandler(BaseHTTPRequestHandler):
    server_version = "AudioDownloader/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        super().end_headers()

    def send_json(
        self,
        payload: Any,
        status: int = HTTPStatus.OK,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def is_authenticated(self) -> bool:
        if not APP_PASSWORD:
            return True
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookies.get("audio_session")
        if not morsel:
            return False
        with SESSIONS_LOCK:
            return morsel.value in SESSIONS

    def require_authentication(self, path: str) -> bool:
        if self.is_authenticated():
            return True
        if path.startswith("/api/"):
            self.send_json({"error": "Sign in to continue."}, HTTPStatus.UNAUTHORIZED)
        else:
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "/login")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
        return False

    def send_file(self, path: Path, download_name: str | None = None) -> None:
        try:
            stat = path.stat()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(stat.st_size))
        if download_name:
            self.send_header("Content-Disposition", content_disposition(download_name))
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        with path.open("rb") as handle:
            shutil.copyfileobj(handle, self.wfile)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "tools": {
                        "youtube": downloader_available("youtube"),
                        "spotify": downloader_available("spotify"),
                        "ffmpeg": shutil.which("ffmpeg") is not None,
                    },
                    "queued": WORK_QUEUE.qsize(),
                }
            )
            return

        public_files = {"/login", "/styles.css", "/login.js", "/favicon.svg"}
        if path not in public_files and not self.require_authentication(path):
            return

        if path == "/login":
            if self.is_authenticated():
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", "/")
                self.end_headers()
                return
            self.send_file(STATIC_ROOT / "login.html")
            return

        if path == "/api/jobs":
            with JOBS_LOCK:
                jobs = sorted(JOBS.values(), key=lambda item: item["created_at"], reverse=True)
                self.send_json({"jobs": [public_job(job) for job in jobs[:100]]})
            return

        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})", path)
        if match:
            with JOBS_LOCK:
                job = JOBS.get(match.group(1))
                if not job:
                    self.send_json({"error": "Job not found."}, HTTPStatus.NOT_FOUND)
                else:
                    self.send_json({"job": public_job(job)})
            return

        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/files/([^/]+)", path)
        if match:
            job_id, encoded_name = match.groups()
            try:
                root = safe_job_dir(job_id)
                relative = Path(unquote(encoded_name))
                target = (root / relative).resolve()
                target.relative_to(root)
                if target.suffix.lower() != ".mp3":
                    raise ValueError("Not an MP3")
            except ValueError:
                self.send_error(HTTPStatus.BAD_REQUEST)
                return
            self.send_file(target, target.name)
            return

        match = re.fullmatch(r"/api/jobs/([0-9a-f]{32})/archive", path)
        if match:
            job_id = match.group(1)
            with JOBS_LOCK:
                job = JOBS.get(job_id)
                if not job or job.get("status") != "complete":
                    self.send_json({"error": "Completed job not found."}, HTTPStatus.NOT_FOUND)
                    return
            files = scan_audio_files(job_id)
            if not files:
                self.send_json({"error": "No audio files found."}, HTTPStatus.NOT_FOUND)
                return
            ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
            archive_path = ARCHIVE_ROOT / f"audio-{job_id[:8]}.zip"
            if not archive_path.exists():
                root = safe_job_dir(job_id)
                with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_STORED) as archive:
                    for item in files:
                        source = (root / item["name"]).resolve()
                        source.relative_to(root)
                        archive.write(source, arcname=item["name"])
            self.send_file(archive_path, archive_path.name)
            return

        static_name = "index.html" if path == "/" else path.lstrip("/")
        if static_name not in {
            "index.html",
            "login.html",
            "styles.css",
            "app.js",
            "login.js",
            "favicon.svg",
        }:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_file(STATIC_ROOT / static_name)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/login":
            self.handle_login()
            return
        if parsed.path == "/api/logout":
            self.handle_logout()
            return
        if not self.require_authentication(parsed.path):
            return
        if parsed.path != "/api/jobs":
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self.send_json({"error": "Invalid request size."}, HTTPStatus.BAD_REQUEST)
            return

        try:
            payload = json.loads(self.rfile.read(length))
            source, url = classify_url(str(payload.get("url", "")))
            quality = str(payload.get("quality", "320"))
            if quality not in QUALITY_OPTIONS:
                raise ValueError("Choose a supported MP3 quality.")
        except (json.JSONDecodeError, AttributeError, ValueError) as exc:
            self.send_json({"error": str(exc) or "Invalid request."}, HTTPStatus.BAD_REQUEST)
            return

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "url": url,
            "source": source,
            "quality": quality,
            "status": "queued",
            "progress": 0,
            "message": "Waiting for the current download…" if WORK_QUEUE.qsize() else "Queued…",
            "created_at": utc_now(),
            "started_at": None,
            "finished_at": None,
            "files": [],
            "log": [],
        }
        with JOBS_LOCK:
            JOBS[job_id] = job
            persist_jobs()
        WORK_QUEUE.put(job_id)
        self.send_json({"job": public_job(job)}, HTTPStatus.CREATED)

    def read_json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Invalid request size.")
        try:
            payload = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as exc:
            raise ValueError("Invalid request.") from exc
        if not isinstance(payload, dict):
            raise ValueError("Invalid request.")
        return payload

    def handle_login(self) -> None:
        if not APP_PASSWORD:
            self.send_json({"ok": True})
            return
        try:
            payload = self.read_json_body()
            supplied = str(payload.get("password", ""))
        except ValueError as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        if not secrets.compare_digest(supplied, APP_PASSWORD):
            time.sleep(0.35)
            self.send_json({"error": "That password is not correct."}, HTTPStatus.UNAUTHORIZED)
            return

        token = secrets.token_urlsafe(32)
        with SESSIONS_LOCK:
            SESSIONS.add(token)
        secure = "; Secure" if SECURE_COOKIES else ""
        cookie = (
            f"audio_session={token}; Path=/; Max-Age=604800; HttpOnly; "
            f"SameSite=Strict{secure}"
        )
        self.send_json({"ok": True}, headers={"Set-Cookie": cookie})

    def handle_logout(self) -> None:
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookies.get("audio_session")
        if morsel:
            with SESSIONS_LOCK:
                SESSIONS.discard(morsel.value)
        secure = "; Secure" if SECURE_COOKIES else ""
        cookie = (
            f"audio_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{secure}"
        )
        self.send_json({"ok": True}, headers={"Set-Cookie": cookie})


def main() -> None:
    for directory in (DOWNLOAD_ROOT, STATE_ROOT, ARCHIVE_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
    load_jobs()
    threading.Thread(target=worker_loop, name="download-worker", daemon=True).start()

    bind = os.getenv("AUDIO_BIND", "127.0.0.1")
    port = int(os.getenv("PORT", os.getenv("AUDIO_PORT", "8000")))
    server = ThreadingHTTPServer((bind, port), AppHandler)
    print(f"Audio Downloader is ready at http://{bind}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping…")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
