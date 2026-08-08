import io
import os
import re
import secrets
import threading
import time
import uuid
import zipfile
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

# In-memory job tracker: job_id -> {status, filename, error, title, batch_id, query}
JOBS = {}
JOBS_LOCK = threading.Lock()

# In-memory batch tracker: batch_id -> [job_id, ...]
BATCHES = {}
BATCHES_LOCK = threading.Lock()

YOUTUBE_URL_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/|music\.youtube\.com/watch\?v=)[\w\-]+"
)

DURATION_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")
BOOLEAN_RE = re.compile(r"^(true|false)$", re.IGNORECASE)


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


def parse_song_list(text: str):
    """Parse a pasted playlist like:
    true
    4:42
    Otra Como Tu
    ErosRamazzotti

    true
    4:11
    ...

    into a list of {"title": ..., "artist": ...} dicts. Tolerant of blocks
    missing the leading true/false or the duration line.
    """
    blocks = re.split(r"\n\s*\n", text.strip())
    songs = []
    for block in blocks:
        lines = [l.strip() for l in block.splitlines() if l.strip()]
        keep = [l for l in lines if not BOOLEAN_RE.match(l) and not DURATION_RE.match(l)]
        if not keep:
            continue
        title = keep[0]
        artist = keep[1] if len(keep) > 1 else ""
        songs.append({"title": title, "artist": artist})
    return songs


def run_download(job_id: str, query: str):
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
        "default_search": "ytsearch1",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(query, download=True)
            if "entries" in info:  # search results come back wrapped
                info = info["entries"][0]
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


def start_job(query: str, label: str, batch_id: str = None) -> str:
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "queued",
            "created": time.time(),
            "label": label,
            "batch_id": batch_id,
        }
    if batch_id:
        with BATCHES_LOCK:
            BATCHES.setdefault(batch_id, []).append(job_id)
    thread = threading.Thread(target=run_download, args=(job_id, query), daemon=True)
    thread.start()
    return job_id


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
    job_id = start_job(url, url)
    return jsonify({"job_id": job_id})


@app.route("/api/batch", methods=["POST"])
@requires_auth
def batch():
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    songs = parse_song_list(text)
    if not songs:
        return jsonify({"error": "Couldn't find any songs in that list."}), 400

    batch_id = uuid.uuid4().hex
    jobs = []
    for song in songs:
        query = " ".join(p for p in [song["artist"], song["title"]] if p).strip()
        label = f'{song["title"]} — {song["artist"]}' if song["artist"] else song["title"]
        job_id = start_job(f"ytsearch1:{query}", label, batch_id=batch_id)
        jobs.append({"job_id": job_id, "label": label})

    return jsonify({"batch_id": batch_id, "jobs": jobs})


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


@app.route("/api/zip/<batch_id>")
@requires_auth
def get_zip(batch_id):
    with BATCHES_LOCK:
        job_ids = list(BATCHES.get(batch_id, []))
    if not job_ids:
        return jsonify({"error": "unknown batch"}), 404

    buf = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        with JOBS_LOCK:
            for job_id in job_ids:
                job = JOBS.get(job_id)
                if not job or job.get("status") != "done":
                    continue
                path = os.path.join(job["job_dir"], job["filename"])
                if not os.path.exists(path):
                    continue
                name = safe_filename(job.get("title", "audio")) + ".mp3"
                base, ext = os.path.splitext(name)
                n = 1
                while name in used_names:
                    n += 1
                    name = f"{base} ({n}){ext}"
                used_names.add(name)
                zf.write(path, arcname=name)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name="playlist.zip", mimetype="application/zip")


PAGE = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>YT → MP3</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 560px; margin: 40px auto; padding: 0 16px; }
  h1 { font-size: 1.4rem; }
  h2 { font-size: 1.05rem; margin-top: 36px; }
  input[type=text], textarea { width: 100%; padding: 10px; font-size: 1rem; box-sizing: border-box; font-family: inherit; }
  textarea { min-height: 160px; resize: vertical; }
  button { margin-top: 10px; padding: 10px 16px; font-size: 1rem; cursor: pointer; }
  #status, #batchStatus { margin-top: 16px; font-size: 0.95rem; }
  .item { padding: 10px 0; border-bottom: 1px solid #8884; }
  a.dl { display: inline-block; margin-top: 6px; }
  .hint { font-size: 0.85rem; opacity: 0.7; margin-top: 4px; }
  #zipBtn { display: none; }
</style>
</head>
<body>
<h1>🎵 YouTube → MP3 (320kbps)</h1>

<input id="url" type="text" placeholder="Paste a single YouTube video URL">
<button onclick="submitUrl()">Convert</button>
<div id="status"></div>

<h2>Download a list by name</h2>
<div class="hint">Paste a list of tracks (title on one line, artist on the next — the true/false and duration lines are ignored automatically). Each track is searched on YouTube and downloaded.</div>
<textarea id="list" placeholder="Otra Como Tu&#10;ErosRamazzotti&#10;&#10;Cosas De La Vida&#10;ErosRamazzotti"></textarea>
<button onclick="submitBatch()">Search &amp; Download All</button>
<button id="zipBtn" onclick="downloadZip()">⬇ Download All as ZIP</button>
<div id="batchStatus"></div>

<script>
let currentBatchId = null;
let batchTotal = 0;
let batchDone = 0;

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
  pollJob(data.job_id, item);
}

async function submitBatch() {
  const text = document.getElementById('list').value;
  if (!text.trim()) return;
  const batchDiv = document.getElementById('batchStatus');
  batchDiv.innerHTML = '';
  document.getElementById('zipBtn').style.display = 'none';

  const res = await fetch('/api/batch', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text})
  });
  const data = await res.json();
  if (data.error) { batchDiv.textContent = 'Error: ' + data.error; return; }

  currentBatchId = data.batch_id;
  batchTotal = data.jobs.length;
  batchDone = 0;

  data.jobs.forEach(j => {
    const item = document.createElement('div');
    item.className = 'item';
    item.textContent = 'Queued: ' + j.label;
    batchDiv.appendChild(item);
    pollJob(j.job_id, item, true);
  });
}

function pollJob(jobId, item, isBatch) {
  const poll = setInterval(async () => {
    const r = await fetch('/api/status/' + jobId);
    const s = await r.json();
    if (s.status === 'error') {
      item.textContent = '❌ Error: ' + s.error;
      clearInterval(poll);
      if (isBatch) batchFinished();
    } else if (s.status === 'done') {
      item.innerHTML = '✅ ' + (s.title || 'audio') +
        '<br><a class="dl" href="/api/file/' + jobId + '">Download MP3</a>';
      clearInterval(poll);
      if (isBatch) batchFinished();
    } else {
      item.textContent = (s.status || 'working') + (s.progress ? ' ' + s.progress : '');
    }
  }, 1500);
}

function batchFinished() {
  batchDone++;
  if (batchDone >= batchTotal) {
    document.getElementById('zipBtn').style.display = 'inline-block';
  }
}

function downloadZip() {
  if (currentBatchId) window.location = '/api/zip/' + currentBatchId;
}
</script>
</body>
</html>
"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8443, debug=False)
