# Cineplex Seat Watcher (The Odyssey · IMAX 70mm · Vaughan)

Standalone watcher — **no Claude, no browser, no app needs to be open.** A tiny
Python script polls Cineplex's public availability API and alerts you when **2
adjacent seats in rows F–J** open up for any *The Odyssey* IMAX 70mm showing at
Cineplex Cinemas Vaughan.

It is **read-only**: it never books, holds, logs in, or pays. When you get an
alert, you book manually (and fast).

## Files
- `watch.py` — the watcher. Runs once per invocation.
- `com.malek.cineplex-seat-watch.plist` — launchd job that runs it every 10 min.
- `state.json` — remembers what it already alerted on (so you're not spammed).
- `layouts/` — cached seat maps (geometry never changes).
- `watch.log` — output from each run.

## How it works
1. Lists all IMAX 70mm showtimes for the film at theatre 7408 (Vaughan) via
   `apis.cineplex.com/.../showtimes`. This endpoint needs Cineplex's public
   web **subscription key** — a value their site ships to every browser (not a
   personal credential). It's set as a default in `watch.py`.
2. For each showtime with ≥2 seats left, reads the keyless
   `seat-layout` + `seat-availability` endpoints and looks for two adjacent
   available **Standard** seats whose row is F, G, H, I, or J.
3. On a *new* match, sends notifications and records it in `state.json`.

## Notifications
- **macOS banner** — always fires (via `osascript`). Requires you to be logged in.
- **Phone push (recommended)** via [ntfy.sh](https://ntfy.sh) — free, no account:
  1. Install the **ntfy** app (iOS/Android).
  2. Pick a private, hard-to-guess topic, e.g. `malek-odyssey-7f3a9c`.
  3. Subscribe to that topic in the app.
  4. Put the same topic in the plist `NTFY_TOPIC` value (or export `NTFY_TOPIC`).
- **Email (optional)** — set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
  (use an **app password**, never your real password), and `EMAIL_TO`.

## Install (run every 10 minutes, forever)
```bash
cp /Users/malekabdullah/cineplex-seat-watch/com.malek.cineplex-seat-watch.plist \
   ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.malek.cineplex-seat-watch.plist
launchctl enable gui/$(id -u)/com.malek.cineplex-seat-watch
```
Check it:
```bash
launchctl list | grep cineplex
tail -f /Users/malekabdullah/cineplex-seat-watch/watch.log
```
Stop / remove:
```bash
launchctl bootout gui/$(id -u)/com.malek.cineplex-seat-watch
```

## Run once by hand
```bash
cd /Users/malekabdullah/cineplex-seat-watch && python3 watch.py
```

## Tuning (env vars or edit the constants at the top of `watch.py`)
- `ACCEPT_ROWS` — default `F,G,H,I,J`.
- `NEED_ADJACENT` — `1` (default) requires side-by-side; `0` = any 2 seats in F–J.
- `THEATRE_ID` / `FILM_ID` — change theatre or movie.
- `INSECURE_TLS=1` — only if you're behind a TLS-inspecting proxy and see
  certificate errors in the log (the script also auto-falls-back and warns).

## Notes / caveats
- Polling, not a webhook — Cineplex offers no push feed. Latency ≈ your interval.
- Be a good citizen: 10-min cadence keeps request volume modest and read-only.
- If Cineplex rotates the subscription key and listing calls start returning 401,
  grab the new key from the site (any browser request to `apis.cineplex.com`
  carries an `Ocp-Apim-Subscription-Key` header) and update `watch.py`.
- The Mac must be powered on and you logged in for launchd to run the job.
