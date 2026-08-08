# YouTube → MP3 (320kbps) — Private Web App

A small password-protected website that converts YouTube videos to 320kbps MP3,
usable from your phone or laptop, in or out of the house, via your DuckDNS domain.

Everything lives on **D:\ytmp3-server** (your C: drive had 0 bytes free).

**Use responsibly:** only convert content you own, have rights to, or that's
royalty-free/Creative Commons. This keeps the login private — do not share the
URL or password publicly.

---

## What's here

- `app.py` — the Flask server (password-protected, calls yt-dlp + bundled ffmpeg)
- `tools/` — bundled ffmpeg (already downloaded)
- `start.ps1` — convenience launcher that asks for a password on first run
- `Caddyfile` — reverse proxy config for free automatic HTTPS

## 1. Run it locally first (sanity check)

```powershell
cd D:\ytmp3-server
$env:YTMP3_PASSWORD = "choose-a-strong-password"
python app.py
```

Visit `http://localhost:8443` on the same PC — it'll ask for a username (anything)
and the password you set. Paste a YouTube URL and try converting.

## 2. Set up DuckDNS (dynamic DNS)

1. Go to https://www.duckdns.org, sign in, create a domain, e.g. `yourname.duckdns.org`.
2. Note your **token** from the DuckDNS dashboard.
3. Keep DuckDNS pointed at your current home IP automatically — create
   `D:\ytmp3-server\duckdns_update.ps1`:

   ```powershell
   $domain = "yourname"
   $token  = "your-duckdns-token"
   Invoke-RestMethod "https://www.duckdns.org/update?domains=$domain&token=$token&ip="
   ```

4. Schedule it to run every 5 minutes via Task Scheduler (Action: `powershell.exe
   -File D:\ytmp3-server\duckdns_update.ps1`, Trigger: repeat every 5 min).

## 3. Forward ports on your router

Log into your router's admin page (usually `192.168.1.1`) and forward:

- External port **80** → this PC's local IP, port 80
- External port **443** → this PC's local IP, port 443

(Find this PC's local IP with `ipconfig`.) Your ISP must not be using CGNAT for
this to work — if port forwarding doesn't reach your PC from outside, that's
the usual cause, and DuckDNS/port-forwarding can't fix it (ask your ISP for a
public IP, or use a tunnel service like Cloudflare Tunnel instead).

## 4. Get free HTTPS with Caddy (recommended — don't skip)

Without HTTPS, your password is sent in plain text (easily readable) every
time you log in from outside your house. Caddy gets you a free certificate
automatically.

```powershell
winget install CaddyServer.Caddy
```

Edit `Caddyfile` in this folder — replace `yourname.duckdns.org` with your real
domain — then run:

```powershell
cd D:\ytmp3-server
caddy run
```

Caddy will get a Let's Encrypt certificate automatically (needs port 80/443
reachable from the internet, which you set up in step 3) and proxy
`https://yourname.duckdns.org` → your app on port 8443.

## 5. Start everything

Each time you want the server up:

```powershell
# Terminal 1
cd D:\ytmp3-server
.\start.ps1

# Terminal 2
cd D:\ytmp3-server
caddy run
```

Then from your phone (on any network) visit `https://yourname.duckdns.org`,
log in, paste a link, download the MP3.

To run automatically on PC startup, add both as Task Scheduler entries
("At log on") instead of running manually.

## Notes

- Files download to `D:\ytmp3-server\downloads\<job-id>\` temporarily and are
  served for download from there — nothing is auto-deleted, so clean that
  folder out occasionally.
- Audio is extracted at true 320kbps CBR MP3 (verified via ffprobe during setup).
- This PC needs to stay on and connected for the site to be reachable from
  outside.
