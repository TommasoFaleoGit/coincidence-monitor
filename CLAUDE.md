# CLAUDE.md — Live SNSPD Coincidence Monitor

## Project Overview

Build a live web dashboard showing coincidence counts from SNSPDs and a Time Tagger.
The university PC runs a Python script that periodically pushes a small JSON file to a
GitHub repository via the GitHub REST API (no git binary, no GitHub account on the PC).
A GitHub Pages static site reads that JSON and displays live counts in the browser.

**Security model:** The university PC makes outbound HTTPS calls only. No ports are opened,
no SSH keys or GitHub accounts are configured on the PC. The public dashboard is strictly
read-only.

---

## Repository Structure

```
coincidence-monitor/          ← GitHub repo root
├── index.html                ← GitHub Pages dashboard (auto-served)
├── data.json                 ← Written by the PC script, read by the page
├── config.json               ← Optional: channel names, display settings
└── publisher/
    ├── publisher.py          ← Main script running on the university PC
    ├── requirements.txt
    └── .env.example          ← Token template (never commit the real .env)
```

---

## Step 1 — GitHub Setup (done once, on your personal machine)

1. Create a new **public** GitHub repository, e.g. `coincidence-monitor`.
2. Enable GitHub Pages:
   - Go to **Settings → Pages → Source → Deploy from branch → main → / (root)**.
   - The dashboard will be live at `https://<your-username>.github.io/coincidence-monitor/`.
3. Create a **Fine-Grained Personal Access Token**:
   - Go to **GitHub Settings → Developer Settings → Personal Access Tokens → Fine-grained tokens**.
   - Click **Generate new token**.
   - Set **Repository access** to `Only select repositories` → pick `coincidence-monitor`.
   - Under **Permissions → Repository permissions → Contents** → set to **Read and Write**.
   - Set an expiry (90 days recommended; you can renew it without touching the PC script).
   - Copy the token (starts with `ghp_...`). You will only see it once.
4. Commit an initial `data.json` to the repo so the file already exists (required for the
   GitHub Contents API update flow):

```json
{
  "timestamp": "1970-01-01T00:00:00Z",
  "channels": {},
  "coincidences": {}
}
```

---

## Step 2 — University PC Setup

### 2a. Install dependencies

```bash
pip install requests python-dotenv
```

No `git` binary needed. No GitHub account needed.

### 2b. Create `.env` in the `publisher/` directory

```
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
GITHUB_REPO=<your-username>/coincidence-monitor
UPDATE_INTERVAL=10
```

- `UPDATE_INTERVAL` is in seconds. Keep it ≥ 5 to avoid hitting GitHub API rate limits
  (authenticated limit is 5000 requests/hour).
- **Never commit `.env`**. Add it to `.gitignore` if using git locally.

### 2c. `publisher/requirements.txt`

```
requests>=2.31.0
python-dotenv>=1.0.0
```

---

## Step 3 — Publisher Script (`publisher/publisher.py`)

Implement the following logic:

### Imports and config

```python
import os, json, time, base64, datetime
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN    = os.getenv("GITHUB_TOKEN")
REPO     = os.getenv("GITHUB_REPO")          # "username/repo"
INTERVAL = int(os.getenv("UPDATE_INTERVAL", 10))
API_BASE = f"https://api.github.com/repos/{REPO}/contents/data.json"
HEADERS  = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}
```

### Reading counts from the Time Tagger

Add a function `read_counts()` that interfaces with your Time Tagger SDK and returns a
dictionary like:

```python
{
  "ch1": 123456,
  "ch2": 234567,
  "coincidences_ch1_ch2": 789,
  # add more channels / coincidence pairs as needed
}
```

This function should be the only part that needs customisation for your specific hardware.
If the Time Tagger SDK is not available, fall back to dummy random data for testing.

### Pushing to GitHub

The GitHub Contents API requires knowing the current file's SHA before updating it.
Implement a `push_data(counts)` function that:

1. **GET** `API_BASE` → extract `sha` from the response JSON.
2. Build the new `data.json` payload:
   ```python
   payload = {
       "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
       "counts": counts,
   }
   ```
3. Base64-encode the JSON string.
4. **PUT** `API_BASE` with body:
   ```json
   {
     "message": "update counts",
     "content": "<base64 encoded JSON>",
     "sha": "<sha from step 1>"
   }
   ```
5. Handle errors gracefully: print warnings but do not crash the loop.

### Main loop

```python
while True:
    counts = read_counts()
    push_data(counts)
    time.sleep(INTERVAL)
```

---

## Step 4 — Dashboard (`index.html`)

Build a single self-contained HTML file with no external dependencies (pure HTML + CSS + JS).

### Behaviour

- On load and every `REFRESH_INTERVAL` seconds (default: 10), fetch `data.json` with a
  cache-busting query parameter (`?t=<timestamp>`).
- Parse the JSON and update the displayed values in-place (no full page reload).
- Show a "last updated" timestamp, formatted in the local timezone.
- Show a subtle animated indicator (e.g. pulsing dot) when counts update successfully.
- Show a warning banner if the last update is more than `STALE_THRESHOLD` seconds old
  (default: 60), indicating the publisher may be offline.

### Layout

- Clean, minimal dark-mode design suitable for a lab monitor.
- One card per channel showing: channel name, current count, and count rate (counts/s,
  estimated from two successive readings).
- A separate section for coincidence pairs.
- A status bar at the bottom: last fetch time, connection status, repo link.

### Config

Read display names and grouping from `config.json` if present, falling back to the raw
keys in `data.json`. Example `config.json`:

```json
{
  "title": "SNSPD Coincidence Monitor",
  "channels": {
    "ch1": "Detector A",
    "ch2": "Detector B"
  },
  "coincidences": {
    "coincidences_ch1_ch2": "A–B Coincidences"
  }
}
```

---

## Step 5 — Security Checklist

Claude Code should verify the following before considering the implementation complete:

- [ ] `.env` is listed in `.gitignore` (or is outside the repo entirely).
- [ ] No token, credential, or machine hostname appears anywhere in committed files.
- [ ] `publisher.py` never starts a server, never opens a socket, never listens on any port.
- [ ] `publisher.py` only makes outbound HTTPS calls to `api.github.com`.
- [ ] `index.html` fetches only `data.json` and optionally `config.json` from the same origin.
- [ ] `index.html` has no forms, no user input, no write operations of any kind.
- [ ] GitHub token scope is **Contents Read+Write on this repo only** — confirm in token settings.

---

## Step 6 — Testing Without Hardware

To test the full pipeline before connecting the Time Tagger:

1. In `publisher.py`, implement `read_counts()` to return random integers (simulated counts).
2. Run `publisher.py` and confirm `data.json` updates on GitHub every `INTERVAL` seconds.
3. Open `https://<username>.github.io/coincidence-monitor/` and confirm counts update live.
4. Simulate a stale feed by stopping `publisher.py` and confirming the warning banner appears.

---

## Step 7 — Running as a Background Service (Optional)

To keep the publisher running on the university PC even after logout:

**Linux (systemd user service):**

Create `~/.config/systemd/user/coincidence-publisher.service`:

```ini
[Unit]
Description=SNSPD Coincidence Publisher

[Service]
WorkingDirectory=/path/to/publisher
ExecStart=/usr/bin/python3 /path/to/publisher/publisher.py
Restart=on-failure
RestartSec=15

[Install]
WantedBy=default.target
```

Then:
```bash
systemctl --user enable coincidence-publisher
systemctl --user start coincidence-publisher
loginctl enable-linger $USER   # keep it running after logout
```

**Windows (Task Scheduler):**

Create a Basic Task that runs `python publisher.py` at system startup with "Run whether
user is logged on or not" unchecked (to avoid needing admin rights).

---

## Rate Limits & Constraints

| Constraint | Value |
|---|---|
| GitHub authenticated API rate limit | 5 000 requests / hour |
| Minimum safe `UPDATE_INTERVAL` | 5 seconds |
| GitHub Pages propagation delay | ~30–60 seconds after push |
| `data.json` recommended max size | < 50 KB |
| GitHub free Pages bandwidth | 100 GB / month |

> **Note on latency:** Because GitHub Pages has a CDN cache, new `data.json` values may
> take 30–60 seconds to appear even if the publisher pushes every 5 seconds. For
> sub-10-second latency, consider switching the backend to Cloudflare Workers + KV
> (a future upgrade path that requires no changes to `index.html` logic, only to
> `publisher.py` and the fetch URL).

---

## Upgrade Path (Future)

If lower latency is needed later, the architecture can be upgraded without redesigning
the dashboard:

- **Cloudflare Workers + KV**: publisher POSTs to a Worker endpoint; page fetches from
  the same Worker. ~1–5 s latency. Free tier: 100 k requests/day.
- **ntfy.sh**: publisher POSTs to `https://ntfy.sh/<topic>`; page subscribes via
  Server-Sent Events. ~1–2 s latency. No account needed.

Both options preserve the same outbound-only security model.
