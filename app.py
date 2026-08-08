import os
import re
import secrets
import threading
import time
import uuid
from functools import wraps

from flask import Flask, request, jsonify, send_file, Response, render_template_string

import yt_dlp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
FFMPEG_DIR = os.path.join(BASE_DIR, "tools", "ffmpeg-9.0-essentials_build", "bin")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- Auth ---
# Password is read from the YTMP3_PASSWORD environment variable.
# Set it before starting the server, e.g. in PowerShell:
#   $env:YTMP3_PASSWORD = "your-strong-password"
AUTH_PASSWORD = os.environ.get("YTMP3_PASSWORD")
if not AUTH_PASSWORD:
    raise SystemExit(
        "ERROR: Set the YTMP3_PASSWORD environment variable before starting the server.\n"
        'Example (PowerShell): $env:YTMP3_PASSWORD = "your-strong-password"'
    )

app = Flask(__name__)

# In-memory job tracker: job_id -> {status, filename, error, title}
JOBS = {}
JOBS_LOCK = threading.Lock()

YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|music\.youtube\.com/watch\?v=)[\w\-]+"
)


def check_auth(password):
    return secrets.compare_digest(password or "", AUTH_PASSWORD)


def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.password):
            return Response(
                "Login required", 401,
                {"WWW-Authenticate": 'Basic realm="YT-MP3"'},
            )
        return f(*args, **kwargs)
    return decorated


def safe_filename(name: str) -> str:
    name = re.sub(r"[^\w\-. ]", "_", name).strip()
    return name[:150] or "audio"


def run_download(job_id: str, url: str):
    job_dir = os.path.join(DOWNLOAD_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    def hook(d):
        if d["status"] == "downloading":
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "downloading"
                p = d.get("_percent_str", "").strip()
                if p:
                    JOBS[job_id]["progress"] = p
        elif d["status"] == "finished":
            with JOBS_LOCK:
                JOBS[job_id]["status"] = "converting"

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(job_dir, "%(title)s.%(ext)s"),
        "ffmpeg_location": FFMPEG_DIR,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }
        ],
        "progress_hooks": [hook],
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "audio")
        mp3_files = [f for f in os.listdir(job_dir) if f.endswith(".mp3")]
        if not mp3_files:
            raise RuntimeError("Conversion finished but no MP3 file was produced.")
        filename = mp3_files[0]
        with JOBS_LOCK:
            JOBS[job_id].update(
                status="done", filename=filename, title=title, job_dir=job_dir
            )
    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id].update(status="error", error=str(e))


@app.route("/")
@requires_auth
def index():
    return render_template_string(PAGE)


@app.route("/api/convert", methods=["POST"])
@requires_auth
def convert():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    if not url or not YOUTUBE_URL_RE.match(url):
        return jsonify({"error": "Please provide a valid YouTube video URL."}), 400

    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "queued", "created": time.time()}

    thread = threading.Thread(target=run_download, args=(job_id, url), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
@requires_auth
def status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "unknown job"}), 404
        return jsonify(
            {
                "status": job.get("status"),
                "progress": job.get("progress"),
                "title": job.get("title"),
                "error": job.get("error"),
            }
        )


@app.route("/api/file/<job_id>")
@requires_auth
def get_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done":
        return jsonify({"error": "file not ready"}), 404
    path = os.path.join(job["job_dir"], job["filename"])
    download_name = safe_filename(job.get("title", "audio")) + ".mp3"
    return send_file(path, as_attachment=True, download_name=download_name)


PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YT → MP3</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 480px; margin: 40px auto; padding: 0 16px; }
  h1 { font-size: 1.4rem; }
  input[type=text] { width: 100%; padding: 10px; font-size: 1rem; box-sizing: border-box; }
  button { margin-top: 10px; padding: 10px 16px; font-size: 1rem; cursor: pointer; }
  #status { margin-top: 16px; font-size: 0.95rem; }
  .item { padding: 10px 0; border-bottom: 1px solid #8884; }
  a.dl { display: inline-block; margin-top: 6px; }
</style>
</head>
<body>
<h1>🎵 YouTube → MP3 (320kbps)</h1>
<input id="url" type="text" placeholder="Paste YouTube video URL">
<button onclick="submitUrl()">Convert</button>
<div id="status"></div>

<script>
async function submitUrl() {
  const url = document.getElementById('url').value.trim();
  if (!url) return;
  const statusDiv = document.getElementById('status');
  const item = document.createElement('div');
  item.className = 'item';
  item.textContent = 'Starting…';
  statusDiv.prepend(item);

  const res = await fetch('/api/convert', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({url})
  });
  const data = await res.json();
  if (data.error) { item.textContent = 'Error: ' + data.error; return; }

  const jobId = data.job_id;
  const poll = setInterval(async () => {
    const r = await fetch('/api/status/' + jobId);
    const s = await r.json();
    if (s.status === 'error') {
      item.textContent = 'Error: ' + s.error;
      clearInterval(poll);
    } else if (s.status === 'done') {
      item.innerHTML = '✅ ' + (s.title || 'audio') +
        '<br><a class="dl" href="/api/file/' + jobId + '">Download MP3</a>';
      clearInterval(poll);
    } else {
      item.textContent = (s.status || 'working') + (s.progress ? ' ' + s.progress : '');
    }
  }, 1500);
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, debug=False)
