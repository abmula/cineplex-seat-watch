#!/usr/bin/env python3
"""
Cineplex seat watcher — standalone, no Claude required.

Watches every "The Odyssey" IMAX 70mm showing at Cineplex Cinemas Vaughan and
alerts when 2 ADJACENT standard seats become available in rows F-J.

READ-ONLY. It only reads Cineplex's public availability API. It never books,
holds, logs in, or pays. When alerted, you book manually and quickly.

Runs on a plain `python3 watch.py` — intended to be fired every ~10 min by
launchd (see com.malek.cineplex-seat-watch.plist) or cron, independent of any app.

Config is via the constants below or environment variables of the same name.
"""

import os, sys, json, re, ssl, gzip, socket, subprocess, urllib.request, urllib.error
from datetime import datetime, timezone

# ---------------------------------------------------------------- config
THEATRE_ID   = os.environ.get("THEATRE_ID", "7408")          # Cineplex Cinemas Vaughan
FILM_ID      = os.environ.get("FILM_ID", "38376")            # The Odyssey (IMAX 70mm variant)
ACCEPT_ROWS  = set((os.environ.get("ACCEPT_ROWS", "F,G,H,I,J")).replace(" ", "").split(","))
NEED_ADJACENT = os.environ.get("NEED_ADJACENT", "1") != "0"  # require 2 seats side-by-side
EXPERIENCE_MATCH = ["IMAX", "70MM"]                          # both must be present (case-insensitive)

# Cineplex ships this Azure APIM subscription key to every browser that loads the
# site. It is NOT a personal credential. It only unlocks the public showtimes list.
# `or` (not a default arg) so an empty env value — e.g. an unset GitHub secret,
# which is injected as "" — falls back to the public browser key rather than breaking auth.
SUBSCRIPTION_KEY = os.environ.get("CINEPLEX_SUBSCRIPTION_KEY") or "dcdac5601d864addbc2675a2e96cb1f8"

# Notifications (all optional; macOS banner is always attempted).
NTFY_TOPIC   = os.environ.get("NTFY_TOPIC", "")              # e.g. "malek-odyssey-7f3a9" -> push to phone via ntfy app
NTFY_SERVER  = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
# Email (SMTP) — leave blank to disable. Use an app password, never your login password.
SMTP_HOST    = os.environ.get("SMTP_HOST", "")
SMTP_PORT    = int(os.environ.get("SMTP_PORT") or "587")   # `or` so an unset ("") secret doesn't crash int()
SMTP_USER    = os.environ.get("SMTP_USER", "")
SMTP_PASS    = os.environ.get("SMTP_PASS", "")
EMAIL_TO     = os.environ.get("EMAIL_TO", "")

BASE = "https://apis.cineplex.com/prod"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")
INSECURE   = os.environ.get("INSECURE_TLS", "0") == "1"      # only if your machine sits behind a TLS-inspecting proxy
TIMEOUT    = 25

# ---------------------------------------------------------------- http
def _ctx():
    if INSECURE:
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

CTX = _ctx()

_FALLBACK_CTX = None  # set once if a TLS-inspecting proxy forces us to skip verification

def _open(req):
    global _FALLBACK_CTX
    try:
        return urllib.request.urlopen(req, timeout=TIMEOUT, context=(_FALLBACK_CTX or CTX))
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError) and _FALLBACK_CTX is None:
            _FALLBACK_CTX = ssl._create_unverified_context()
            log("WARNING: TLS verification failed (inspecting proxy?); falling back to unverified for this host")
            return urllib.request.urlopen(req, timeout=TIMEOUT, context=_FALLBACK_CTX)
        raise

def get_json(url, use_key=False):
    headers = {"Accept": "application/json", "Accept-Language": "en",
               "User-Agent": "Mozilla/5.0 (Macintosh; personal-seat-watch)"}
    if use_key:
        headers["Ocp-Apim-Subscription-Key"] = SUBSCRIPTION_KEY
    req = urllib.request.Request(url, headers=headers)
    with _open(req) as r:
        data = r.read()
        if r.headers.get("content-encoding") == "gzip":
            data = gzip.decompress(data)
        return json.loads(data)

# ---------------------------------------------------------------- logic
def log(msg):
    print(f"{datetime.now().isoformat(timespec='seconds')}  {msg}", flush=True)

def enumerate_70mm_sessions():
    """Return list of dicts: {id, date, time, seatsRemaining, isSoldOut, seatMapUrl, deeplink}."""
    url = f"{BASE}/cpx/theatrical/api/v1/showtimes?LocationId={THEATRE_ID}&FilmId={FILM_ID}&language=en"
    listing = get_json(url, use_key=True)
    want = set(x.upper() for x in EXPERIENCE_MATCH)
    out = []
    for th in listing:
        for d in th.get("dates", []):
            for m in d.get("movies", []):
                for exp in m.get("experiences", []):
                    et = set(x.upper() for x in exp.get("experienceTypes", []))
                    if not want.issubset(et):
                        continue
                    for s in exp.get("sessions", []):
                        sid = str(s.get("vistaSessionId") or "")
                        if not sid:
                            mm = re.search(r"showtimeId=(\d+)", s.get("seatMapUrl", ""))
                            sid = mm.group(1) if mm else ""
                        if not sid:
                            continue
                        out.append({
                            "id": sid,
                            "date": (s.get("showStartDateTime") or d.get("startDate") or "")[:10],
                            "time": (s.get("showStartDateTime") or "")[11:16],
                            "seatsRemaining": s.get("seatsRemaining"),
                            "isSoldOut": s.get("isSoldOut"),
                            "inPast": s.get("isInThePast"),
                            "online": s.get("isShowtimeEnabledOnline"),
                            "seatMapUrl": f"https://www.cineplex.com/ticketing/preview?theatreId={THEATRE_ID}&showtimeId={sid}&dbox=false",
                            "deeplink": s.get("deeplinkUrl", ""),
                        })
    return out

LAYOUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "layouts")

def get_layout(sid):
    """Seat layout never changes for a showtime, so cache it to disk."""
    path = os.path.join(LAYOUT_DIR, f"{sid}.json")
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        pass
    layout = get_json(f"{BASE}/ticketing/api/v1/theatre/{THEATRE_ID}/showtime/{sid}/seat-layout")
    try:
        os.makedirs(LAYOUT_DIR, exist_ok=True)
        with open(path, "w") as f:
            json.dump(layout, f)
    except Exception:
        pass
    return layout

def qualifying_pairs(sid):
    """Return list of 'F12+F13' style adjacent available Standard pairs in ACCEPT_ROWS."""
    layout = get_layout(sid)
    avail  = get_json(f"{BASE}/ticketing/api/v1/theatre/{THEATRE_ID}/showtime/{sid}/seat-availability")
    sa = avail.get("seatAvailabilities", {})
    pairs, singles = [], []
    for area in ("standardSeats", "dboxSeats", "balconySeats"):
        block = layout.get(area) or {}
        for row in block.get("rows", []):
            if row.get("label") not in ACCEPT_ROWS:
                continue
            seats = sorted([s for s in row.get("seats", []) if s.get("type") == "Standard"],
                           key=lambda s: s.get("column", 0))
            avail_seats = [s for s in seats if sa.get(s["id"]) == "Available"]
            singles += [s["label"] for s in avail_seats]
            for i in range(len(seats) - 1):
                a, b = seats[i], seats[i + 1]
                if (b.get("column", 0) - a.get("column", 0) == 1
                        and sa.get(a["id"]) == "Available" and sa.get(b["id"]) == "Available"):
                    pairs.append(f'{a["label"]}+{b["label"]}')
    total_avail = sum(1 for v in sa.values() if v == "Available")
    return pairs, singles, total_avail

# ---------------------------------------------------------------- state
def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {"alerted": {}}

def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)

# ---------------------------------------------------------------- notify
def notify_macos(title, message):
    try:
        subprocess.run(["osascript", "-e",
                        f'display notification {json.dumps(message)} with title {json.dumps(title)} sound name "Glass"'],
                       check=False)
    except Exception as e:
        log(f"macOS notify failed: {e}")

def notify_ntfy(title, message, click_url):
    if not NTFY_TOPIC:
        return
    try:
        req = urllib.request.Request(
            f"{NTFY_SERVER}/{NTFY_TOPIC}",
            data=message.encode("utf-8"),
            headers={"Title": title, "Priority": "urgent", "Tags": "clapper",
                     "Click": click_url},
            method="POST")
        urllib.request.urlopen(req, timeout=15, context=CTX)
    except Exception as e:
        log(f"ntfy notify failed: {e}")

def notify_email(subject, html):
    if not (SMTP_HOST and SMTP_USER and SMTP_PASS and EMAIL_TO):
        return
    import smtplib
    from email.mime.text import MIMEText
    msg = MIMEText(html, "html")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=25) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [EMAIL_TO], msg.as_string())
    except Exception as e:
        log(f"email notify failed: {e}")

# ---------------------------------------------------------------- main
def main():
    st = load_state()
    alerted = st.get("alerted", {})
    today = datetime.now().strftime("%Y-%m-%d")

    try:
        sessions = enumerate_70mm_sessions()
    except Exception as e:
        log(f"enumerate failed: {e}")
        st["lastError"] = str(e); save_state(st); return

    log(f"{len(sessions)} IMAX 70mm sessions listed")
    new_hits = []
    checked = 0

    for s in sessions:
        if s.get("inPast") or s.get("online") is False:
            continue
        sr = s.get("seatsRemaining")
        # politeness: skip fetching a seat map when there can't be 2 seats
        if isinstance(sr, int) and sr < 2:
            continue
        try:
            pairs, singles, total = qualifying_pairs(s["id"])
            checked += 1
        except Exception as e:
            log(f"  [{s['id']} {s['date']} {s['time']}] check failed: {e}")
            continue

        match_pairs = pairs if NEED_ADJACENT else ([f"{x}" for x in singles] if len(singles) >= 2 else [])
        if not match_pairs:
            continue

        prev = set(alerted.get(s["id"], []))
        fresh = [p for p in match_pairs if p not in prev]
        if fresh:
            s["pairs"] = match_pairs
            s["fresh"] = fresh
            new_hits.append(s)
            alerted[s["id"]] = sorted(set(match_pairs) | prev)

    log(f"checked {checked} seat maps; {len(new_hits)} new qualifying show(s)")

    # prune past showtimes from state
    alerted = {sid: v for sid, v in alerted.items()
               if any(x["id"] == sid for x in sessions)}
    st["alerted"] = alerted
    st["lastRun"] = datetime.now().isoformat(timespec="seconds")
    st.pop("lastError", None)
    save_state(st)

    if not new_hits:
        return

    # ---- build alerts
    new_hits.sort(key=lambda x: (x["date"], x["time"]))
    first = new_hits[0]
    kind = "adjacent" if NEED_ADJACENT else "in rows F-J"
    if len(new_hits) == 1:
        title = "🎬 Odyssey IMAX 70mm — seats open!"
        short = f'{first["date"]} {first["time"]} @ Vaughan: {kind} {", ".join(first["fresh"][:3])}. Book now.'
    else:
        title = f"🎬 Odyssey IMAX 70mm — {len(new_hits)} shows have seats!"
        short = f'Earliest {first["date"]} {first["time"]}: {", ".join(first["fresh"][:2])}. +{len(new_hits)-1} more. Book now.'

    rows_html = "".join(
        f'<li><b>{h["date"]} {h["time"]}</b> — rows F–J: '
        f'{", ".join(h["pairs"])} '
        f'(<a href="{h["seatMapUrl"]}">seat map / book</a>)</li>'
        for h in new_hits)
    html = (f"<p>Available {kind} seats for <b>The Odyssey — IMAX 70mm</b> at "
            f"Cineplex Cinemas Vaughan:</p><ul>{rows_html}</ul>"
            f"<p style='color:#888'>Read-only alert. Seats go fast — book manually and quickly. "
            f"This watcher never books or holds seats.</p>")

    log("ALERT: " + short)
    notify_macos(title, short)
    notify_ntfy(title, short, first["seatMapUrl"])
    notify_email(title, html)

if __name__ == "__main__":
    socket.setdefaulttimeout(TIMEOUT)
    main()
