#!/usr/bin/env python3
"""Read-only web dashboard for the Linux Self-Healing Lab.

Shows the monitor's current status, CPU/memory history, incidents with their
measured recovery intervals, planned restarts and recent log lines.

Design:
  * Read-only. It only reads <data-dir>/status.json and incidents.jsonl
    (written by the monitor). There are no endpoints that change anything.
  * Loopback only. It refuses to bind to anything but 127.0.0.1 or ::1, and
    rejects requests whose Host header is not a loopback name (protects
    against DNS-rebinding: a web page you visit cannot use your browser to
    read this dashboard through a name it controls).
  * Untrusted text is escaped. Log lines and causes are HTML-escaped on the
    server; the page's script inserts text with textContent only.
  * Strict Content-Security-Policy: no inline scripts or styles, no external
    resources.
  * Never claims "healthy" from old data: if status.json has not been updated
    recently, the page says the monitor data is stale.

Standard library only. Python 3.8+.
"""

import argparse
import html
import ipaddress
import json
import logging
import math
import os
import signal
import socket
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional, Tuple

log = logging.getLogger("dashboard")

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC_FILES = {
    "/static/dashboard.css": ("dashboard.css", "text/css; charset=utf-8"),
    "/static/dashboard.js": ("dashboard.js", "text/javascript; charset=utf-8"),
}
MAX_STATUS_BYTES = 5_000_000
INCIDENT_TAIL_BYTES = 512_000
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
                               "img-src 'self'; connect-src 'self'; base-uri 'none'; "
                               "form-action 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# monitor status -> (status token, icon, label). Color never carries meaning alone.
STATUS_STYLE = {
    "healthy": ("good", "✓", "Healthy"),
    "degraded": ("warning", "!", "Degraded (one failed check)"),
    "transitioning": ("warning", "↻", "Starting or stopping"),
    "stopped": ("neutral", "■", "Stopped (planned)"),
    "down": ("critical", "✕", "Down"),
    "failed": ("critical", "✕", "Failed: systemd stopped restarting it"),
    "monitor-error": ("serious", "?", "Monitor cannot query systemd"),
    "unknown": ("neutral", "?", "Unknown"),
}
CLASS_STYLE = {
    "failure": ("critical", "✕", "Failure"),
    "uncertain": ("warning", "?", "Uncertain"),
}


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


# --------------------------------------------------------------------------
# Reading the monitor's files
# --------------------------------------------------------------------------

def _access_hint(path: str) -> str:
    return ("permission denied reading %s. The dashboard must run as the 'shlab-dash' "
            "account (see docs/DASHBOARD.md)." % path)


def read_status(data_dir: str, now: Optional[float] = None) -> dict:
    """Return {"data": dict|None, "error": str|None, "age_s": float|None}."""
    now = time.time() if now is None else now
    path = os.path.join(data_dir, "status.json")
    try:
        st = os.stat(path)
        if st.st_size > MAX_STATUS_BYTES:
            return {"data": None, "error": "%s is unexpectedly large" % path, "age_s": None}
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"data": None, "age_s": None,
                "error": "%s not found. Is demo-monitor running?" % path}
    except PermissionError:
        return {"data": None, "age_s": None, "error": _access_hint(path)}
    except (OSError, ValueError) as exc:
        return {"data": None, "age_s": None, "error": "cannot read %s: %s" % (path, exc)}
    if not isinstance(data, dict):
        return {"data": None, "age_s": None, "error": "%s has unexpected content" % path}
    return {"data": data, "error": None, "age_s": max(0.0, now - st.st_mtime)}


def read_incidents(data_dir: str, limit: int = 50) -> dict:
    """Newest-first incidents. A partially written last line is skipped."""
    path = os.path.join(data_dir, "incidents.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - INCIDENT_TAIL_BYTES))
            raw = f.read().decode("utf-8", "replace")
    except FileNotFoundError:
        return {"items": [], "error": None, "skipped": 0, "truncated": False}
    except PermissionError:
        return {"items": [], "error": _access_hint(path), "skipped": 0, "truncated": False}
    except OSError as exc:
        return {"items": [], "error": "cannot read %s: %s" % (path, exc), "skipped": 0,
                "truncated": False}
    lines = raw.splitlines()
    truncated = size > INCIDENT_TAIL_BYTES
    if truncated:
        lines = lines[1:]  # first line is probably cut in half
    items, skipped = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except ValueError:
            skipped += 1
            continue
        if isinstance(item, dict):
            items.append(item)
    items.reverse()
    return {"items": items[:limit], "error": None, "skipped": skipped,
            "truncated": truncated or len(items) > limit}


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

def num(x) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) else None


def fmt_s(x) -> str:
    x = num(x)
    return "—" if x is None else "%.3f s" % x


def fmt_time(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return str(iso)


def fmt_clock(iso: Optional[str]) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return ""


def fmt_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return "%.0f s ago" % seconds
    if seconds < 3600:
        return "%.0f min ago" % (seconds / 60)
    return "%.1f h ago" % (seconds / 3600)


def nice_ceil(v: float) -> float:
    """Smallest 'clean' axis maximum >= v (1, 2, 2.5, 5 x 10^n)."""
    if v <= 0:
        return 1.0
    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if m * exp >= v - 1e-12:
            return m * exp
    return 10 * exp


def badge(token: str, icon: str, label: str, extra_class: str = "") -> str:
    return ('<span class="badge badge-%s %s"><span class="badge-icon" aria-hidden="true">'
            '%s</span>%s</span>' % (esc(token), extra_class, esc(icon), esc(label)))


# --------------------------------------------------------------------------
# Charts (server-rendered SVG; dashboard.js adds the hover/keyboard layer)
# --------------------------------------------------------------------------

CHART_W, CHART_H = 640, 168
PAD_L, PAD_R, PAD_T, PAD_B = 48, 72, 12, 24


def line_chart(chart_id: str, title: str, points: List[Tuple[str, Optional[float]]],
               unit: str, decimals: int = 1, min_max: float = 1.0,
               note: str = "") -> str:
    """One series, one axis. Gaps where the value is unknown."""
    values = [v for _, v in points if v is not None]
    head = ('<section class="card chart-card" aria-labelledby="%s-title">'
            '<h3 id="%s-title">%s</h3>' % (chart_id, chart_id, esc(title)))
    if note:
        head += '<p class="card-note">%s</p>' % esc(note)
    if not values:
        return head + '<p class="empty">No data yet.</p></section>'

    ymax = nice_ceil(max(max(values) * 1.15, min_max))
    x0, x1 = PAD_L, CHART_W - PAD_R
    y0, y1 = CHART_H - PAD_B, PAD_T            # y0 = baseline (bottom)
    n = len(points)

    def xy(i, v):
        x = x0 + (x1 - x0) * (i / (n - 1) if n > 1 else 1.0)
        return x, y0 - (y0 - y1) * (v / ymax)

    # gridlines + y tick labels (0, half, max)
    parts = []
    for frac in (0.0, 0.5, 1.0):
        y = y0 - (y0 - y1) * frac
        cls = "axis" if frac == 0 else "grid"
        parts.append('<line class="%s" x1="%d" x2="%d" y1="%.1f" y2="%.1f"/>'
                     % (cls, x0, x1, y, y))
        parts.append('<text class="tick" x="%d" y="%.1f" text-anchor="end" '
                     'dominant-baseline="middle">%s</text>'
                     % (x0 - 6, y, esc(("%." + str(decimals) + "f") % (ymax * frac))))
    # x labels: first and last time
    parts.append('<text class="tick" x="%d" y="%d" text-anchor="start">%s</text>'
                 % (x0, CHART_H - 6, esc(fmt_clock(points[0][0]))))
    parts.append('<text class="tick" x="%d" y="%d" text-anchor="end">%s</text>'
                 % (x1, CHART_H - 6, esc(fmt_clock(points[-1][0]))))

    # series: split into segments at gaps
    segments, cur = [], []
    for i, (_, v) in enumerate(points):
        if v is None:
            if cur:
                segments.append(cur)
            cur = []
        else:
            cur.append(xy(i, v))
    if cur:
        segments.append(cur)
    for seg in segments:
        line = " ".join("%.1f,%.1f" % p for p in seg)
        area = "M%.1f,%.1f " % (seg[0][0], y0) + " ".join("L%.1f,%.1f" % p for p in seg) \
               + " L%.1f,%.1f Z" % (seg[-1][0], y0)
        parts.append('<path class="area" d="%s"/>' % area)
        if len(seg) == 1:
            parts.append('<circle class="point" cx="%.1f" cy="%.1f" r="2"/>' % seg[0])
        else:
            parts.append('<polyline class="line" points="%s"/>' % line)

    # end marker + direct label on the latest value
    last_i = max(i for i, (_, v) in enumerate(points) if v is not None)
    lx, ly = xy(last_i, points[last_i][1])
    latest = ("%." + str(decimals) + "f %s") % (points[last_i][1], unit)
    parts.append('<circle class="end-dot" cx="%.1f" cy="%.1f" r="4"/>' % (lx, ly))
    parts.append('<text class="end-label" x="%.1f" y="%.1f" dominant-baseline="middle">%s'
                 '</text>' % (lx + 8, ly, esc(latest)))
    # hover layer (moved by dashboard.js)
    parts.append('<line class="crosshair" x1="0" x2="0" y1="%d" y2="%d"/>' % (y1, y0))
    parts.append('<circle class="hover-dot" cx="0" cy="0" r="4"/>')

    summary = "%s, %d samples. Latest %s, maximum %s." % (
        title, n, latest, ("%." + str(decimals) + "f %s") % (max(values), unit))
    data_points = json.dumps([[fmt_clock(t), v] for t, v in points])
    geom = json.dumps({"x0": x0, "x1": x1, "y0": y0, "y1": y1, "ymax": ymax, "n": n,
                       "decimals": decimals})
    svg = ('<svg class="chart" viewBox="0 0 %d %d" role="img" tabindex="0" '
           'aria-label="%s" data-points="%s" data-geom="%s" data-unit="%s">%s</svg>'
           % (CHART_W, CHART_H, esc(summary + " Use left and right arrow keys to read values."),
              esc(data_points), esc(geom), esc(unit), "".join(parts)))
    tooltip = ('<div class="tooltip" aria-hidden="true"><span class="tip-value"></span>'
               '<span class="tip-label"></span></div>')
    return head + '<div class="chart-wrap">' + svg + tooltip + '</div></section>'


# --------------------------------------------------------------------------
# Page sections
# --------------------------------------------------------------------------

def stat_tile(label: str, value: str, sub: str = "") -> str:
    return ('<div class="tile"><div class="tile-label">%s</div><div class="tile-value">%s'
            '</div>%s</div>' % (esc(label), esc(value),
                                '<div class="tile-sub">%s</div>' % esc(sub) if sub else ""))


def recovery_cell(inc: dict) -> str:
    lo, hi = num(inc.get("recovery_time_lower_bound_s")), num(inc.get("recovery_time_s"))
    basis = inc.get("timing_basis")
    upper_last_ok = num(inc.get("recovery_upper_bound_from_last_ok_s"))
    if hi is None:
        if upper_last_ok is not None:
            return ('<span class="muted">not measured</span><div class="cell-note">no exit '
                    'time recorded; %s from the last healthy poll to healthy again</div>'
                    % esc("%.3f s" % upper_last_ok))
        return '<span class="muted">not measured</span>'
    main = ("%.3f–%.3f s" % (lo, hi)) if lo is not None else ("≤ %.3f s" % hi)
    if basis == "first-unhealthy-poll":
        note = "measured from the first failed check, not from the start of the problem"
        gap = num(inc.get("undetected_window_before_estimate_s"))
        if gap is not None:
            note += "; it may have started up to %.3f s earlier, or before that" % gap
        return '<span class="num">%s</span><div class="cell-note">%s</div>' % (esc(main),
                                                                              esc(note))
    note = "from systemd's recorded exit to healthy"
    rd = num(inc.get("restart_delay_s"))
    if rd is not None:
        note += "; restart delay %.3f s" % rd
    return '<span class="num">%s</span><div class="cell-note">%s</div>' % (esc(main), esc(note))


def incident_rows(items: List[dict]) -> str:
    rows = []
    for inc in items:
        cls = inc.get("classification", "failure")
        token, icon, label = CLASS_STYLE.get(cls, ("neutral", "?", str(cls)))
        when = inc.get("failed_at") or inc.get("detected_at")
        what = esc(inc.get("kind", ""))
        if inc.get("cause"):
            what += '<div class="cell-note">%s</div>' % esc(inc["cause"])
        if inc.get("gave_up"):
            what += '<div class="cell-note">systemd hit its restart limit</div>'
        pid = "%s → %s" % (inc.get("old_pid", "?"), inc.get("new_pid", "?"))
        details_id = "inc-%s" % esc(inc.get("id", "x"))
        logs = inc.get("logs") or []
        details = ('<details id="%s"><summary>Evidence and logs</summary>'
                   '<p class="cell-note">%s</p><pre class="log">%s</pre></details>'
                   % (details_id, esc(inc.get("evidence", "")),
                      esc("\n".join(str(l) for l in logs)) or "(no log lines captured)"))
        rows.append(
            "<tr><td class=\"num\">%s</td><td>%s</td><td>%s</td><td>%s%s</td>"
            "<td class=\"num\">%s</td><td class=\"num\">%s</td><td>%s</td><td>%s</td></tr>"
            % (esc(inc.get("id")), esc(fmt_time(when)), badge(token, icon, label), what,
               details, esc(pid), esc(fmt_s(inc.get("time_to_detect_s"))),
               recovery_cell(inc), esc(inc.get("recovered_by", ""))))
    return "".join(rows)


def render_page(status: dict, incidents: dict, stale_after: float, data_dir: str) -> str:
    data = status.get("data") or {}
    cur = data.get("current") or {}
    stale = status.get("age_s") is not None and status["age_s"] > stale_after

    # --- header badge ------------------------------------------------------
    if status.get("error"):
        head_badge = badge("serious", "?", "No monitor data")
    elif stale:
        head_badge = badge("serious", "⏱", "Monitor data is stale")
    else:
        head_badge = badge(*STATUS_STYLE.get(data.get("status"), STATUS_STYLE["unknown"]),
                           extra_class="badge-large")

    alerts = []
    if status.get("error"):
        alerts.append(status["error"])
    if stale:
        alerts.append("status.json was last updated %s. The monitor may not be running "
                      "(check: systemctl status demo-monitor). Values below are old."
                      % fmt_age(status["age_s"]))
    if incidents.get("error"):
        alerts.append(incidents["error"])
    if incidents.get("skipped"):
        alerts.append("%d incident line(s) could not be parsed and were skipped."
                      % incidents["skipped"])
    alert_html = "".join('<p class="alert" role="alert">%s</p>' % esc(a) for a in alerts)

    # --- tiles -------------------------------------------------------------
    mem = num(cur.get("mem_bytes"))
    mem_max = num(cur.get("mem_max"))
    tiles = "".join([
        stat_tile("Main PID", str(cur.get("pid") or "—")),
        stat_tile("Automatic restarts", str(cur.get("nrestarts", "—")),
                  "systemd NRestarts (reset by manual start)"),
        stat_tile("CPU", "—" if num(cur.get("cpu_pct")) is None
                  else "%.1f %%" % cur["cpu_pct"], "of one core"),
        stat_tile("Memory", "—" if mem is None else "%.1f MiB" % (mem / 2 ** 20),
                  "" if mem_max is None else "limit %.0f MiB" % (mem_max / 2 ** 20)),
        stat_tile("Health check", "—" if num(cur.get("health_ms")) is None
                  else "%.1f ms" % cur["health_ms"],
                  "OK" if cur.get("health_ok") else (cur.get("health_error") or "")),
    ])

    # --- charts ------------------------------------------------------------
    history = data.get("history") or []
    cpu_pts = [(h.get("t"), num(h.get("cpu_pct"))) for h in history]
    mem_pts = [(h.get("t"), None if num(h.get("mem_bytes")) is None
                else h["mem_bytes"] / 2 ** 20) for h in history]
    charts = (line_chart("cpu", "CPU usage (% of one core)", cpu_pts, "%", 1, 5.0,
                         "CPUQuota caps the service at 50 %.")
              + line_chart("mem", "Memory (MiB)", mem_pts, "MiB", 1, 10.0,
                           "" if mem_max is None else "MemoryMax is %.0f MiB."
                           % (mem_max / 2 ** 20)))
    sample_rows = "".join(
        '<tr><td>%s</td><td class="num">%s</td><td class="num">%s</td>'
        '<td class="num">%s</td><td>%s</td></tr>'
        % (esc(fmt_clock(h.get("t"))),
           esc("" if num(h.get("cpu_pct")) is None else "%.1f" % h["cpu_pct"]),
           esc("" if num(h.get("mem_bytes")) is None else "%.1f" % (h["mem_bytes"] / 2 ** 20)),
           esc("" if num(h.get("health_ms")) is None else "%.1f" % h["health_ms"]),
           "yes" if h.get("healthy") else "no")
        for h in reversed(history))
    samples_table = ('<details id="samples"><summary>Show chart data as a table</summary>'
                     '<div class="table-scroll"><table><thead><tr><th scope="col">Time</th>'
                     '<th scope="col">CPU %%</th><th scope="col">Memory MiB</th>'
                     '<th scope="col">Health ms</th><th scope="col">Healthy</th></tr></thead>'
                     '<tbody>%s</tbody></table></div></details>' % sample_rows)

    # --- open incident -----------------------------------------------------
    open_inc = data.get("open_incident")
    open_html = ""
    if open_inc:
        open_html = ('<section class="card open-incident" aria-labelledby="open-title">'
                     '<h2 id="open-title">%s Incident #%s in progress</h2>'
                     '<p>%s. Detected %s. %s</p></section>'
                     % (badge("critical", "✕", "Open"), esc(open_inc.get("id")),
                        esc(open_inc.get("kind")), esc(fmt_time(open_inc.get("detected_at"))),
                        esc(open_inc.get("cause") or open_inc.get("health_error") or "")))

    # --- incidents table ---------------------------------------------------
    items = incidents.get("items") or []
    if items:
        inc_table = ('<div class="table-scroll"><table class="incidents"><thead><tr>'
                     '<th scope="col">#</th><th scope="col">Time</th>'
                     '<th scope="col">Classification</th><th scope="col">What happened</th>'
                     '<th scope="col">PID</th><th scope="col">Detected after</th>'
                     '<th scope="col">Recovery interval</th><th scope="col">Recovered by</th>'
                     '</tr></thead><tbody>%s</tbody></table></div>' % incident_rows(items))
        if incidents.get("truncated"):
            inc_table += '<p class="card-note">Showing the most recent %d incidents.</p>' % len(items)
    else:
        inc_table = '<p class="empty">No incidents recorded yet.</p>'

    planned = data.get("planned_events") or []
    planned_html = ("<ul class=\"plain\">%s</ul>" % "".join(
        "<li>%s: PID %s → %s <span class=\"cell-note\">(%s)</span></li>"
        % (esc(fmt_time(e.get("t"))), esc(e.get("old_pid")), esc(e.get("new_pid")),
           esc(e.get("evidence")))
        for e in reversed(planned))) if planned else '<p class="empty">None since the monitor started.</p>'

    logs = data.get("recent_logs") or []
    logs_html = '<pre class="log">%s</pre>' % esc("\n".join(str(l) for l in logs)) \
        if logs else '<p class="empty">No log lines.</p>'

    updated = ("Updated %s (%s)" % (fmt_time(data.get("updated")), fmt_age(status.get("age_s")))
               if data else "")

    live = """<main id="live">
<header class="page-head">
  <div><h1>Linux Self-Healing Lab</h1>
  <p class="muted">Service <code>%(unit)s</code> &middot; %(updated)s</p></div>
  <div class="head-status">%(badge)s</div>
</header>
%(alerts)s
%(open)s
<section aria-label="Current values" class="tiles">%(tiles)s</section>
<div class="charts">%(charts)s</div>
%(samples)s
<section class="card" aria-labelledby="inc-title">
  <h2 id="inc-title">Incidents</h2>
  <p class="card-note">Newest first. Each recovery interval is from a single incident, not an average.
  It is a window, not an exact value: the monitor only checks about once per second.
  For crashes the window starts at systemd's recorded exit, and recovery happened within it.
  For hangs (&ldquo;measured from the first failed check&rdquo;) the window starts when the
  monitor first saw the problem, so it may leave out the earlier part of the outage.</p>
  %(incidents)s
</section>
<section class="card" aria-labelledby="planned-title">
  <h2 id="planned-title">Planned restarts</h2>%(planned)s
</section>
<section class="card" aria-labelledby="logs-title">
  <h2 id="logs-title">Recent demo service log</h2>%(logs)s
</section>
<footer class="muted">Read-only view of %(data_dir)s. Times are local.</footer>
</main>""" % {
        "unit": esc(data.get("unit", "demo-service.service")), "updated": esc(updated),
        "badge": head_badge, "alerts": alert_html, "open": open_html, "tiles": tiles,
        "charts": charts, "samples": samples_table, "incidents": inc_table,
        "planned": planned_html, "logs": logs_html, "data_dir": esc(data_dir)}

    return """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Self-Healing Lab dashboard</title>
<link rel="stylesheet" href="/static/dashboard.css">
<script src="/static/dashboard.js" defer></script>
<noscript><meta http-equiv="refresh" content="5"></noscript>
</head><body><div class="viz-root">
<p id="conn" class="alert" role="status" hidden>Dashboard not reachable. Showing the last data received.</p>
%s
</div></body></html>""" % live


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def allowed_hosts(port: int) -> set:
    names = {"127.0.0.1", "localhost", "[::1]"}
    hosts = {"%s:%d" % (n, port) for n in names}
    if port == 80:
        hosts |= names
    return hosts


class Handler(BaseHTTPRequestHandler):
    server_version = "shlab-dashboard"
    sys_version = ""

    def _send(self, code: int, body: bytes, ctype: str, head: bool = False, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _text(self, code: int, msg: str, head: bool = False, extra=None):
        self._send(code, (msg + "\n").encode(), "text/plain; charset=utf-8", head, extra)

    def _handle(self, head: bool):
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.server.allowed_hosts:
            # DNS-rebinding protection: only loopback names are accepted.
            return self._text(403, "Forbidden: unexpected Host header", head)
        path = self.path.split("?", 1)[0]
        cfg = self.server.cfg
        if path == "/":
            status = read_status(cfg["data_dir"])
            incidents = read_incidents(cfg["data_dir"])
            page = render_page(status, incidents, cfg["stale_after"], cfg["data_dir"])
            return self._send(200, page.encode(), "text/html; charset=utf-8", head)
        if path == "/api/status":
            status = read_status(cfg["data_dir"])
            body = {"error": status["error"], "age_s": status["age_s"],
                    "stale": status["age_s"] is not None and status["age_s"] > cfg["stale_after"],
                    "status": status["data"]}
            return self._send(200, json.dumps(body).encode(), "application/json", head)
        if path == "/api/incidents":
            body = read_incidents(cfg["data_dir"])
            return self._send(200, json.dumps(body).encode(), "application/json", head)
        if path in STATIC_FILES:
            name, ctype = STATIC_FILES[path]
            try:
                with open(os.path.join(STATIC_DIR, name), "rb") as f:
                    return self._send(200, f.read(), ctype, head)
            except OSError:
                return self._text(500, "static file missing", head)
        return self._text(404, "Not found", head)

    def do_GET(self):
        self._handle(head=False)

    def do_HEAD(self):
        self._handle(head=True)

    def _not_allowed(self):
        self._text(405, "Method not allowed: this dashboard is read-only",
                   extra={"Allow": "GET, HEAD"})

    do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)


def check_loopback(host: str) -> int:
    """Return the address family for a loopback IP literal, or raise ValueError."""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("--host must be an IP address (127.0.0.1 or ::1), got %r" % host)
    if not ip.is_loopback:
        raise ValueError("refusing to listen on non-loopback address %s" % host)
    return socket.AF_INET6 if ip.version == 6 else socket.AF_INET


def make_server(host: str, port: int, data_dir: str, stale_after: float) -> ThreadingHTTPServer:
    family = check_loopback(host)

    class Server(ThreadingHTTPServer):
        address_family = family
        daemon_threads = True

    srv = Server((host, port), Handler)
    srv.allowed_hosts = allowed_hosts(srv.server_address[1])
    srv.cfg = {"data_dir": data_dir, "stale_after": stale_after}
    return srv


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Self-Healing Lab dashboard (read-only)")
    ap.add_argument("--data-dir", default="./data")
    ap.add_argument("--host", default="127.0.0.1", help="loopback IP only")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--stale-after", type=float, default=5.0,
                    help="seconds without a status update before data is shown as stale")
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        srv = make_server(args.host, args.port, args.data_dir, args.stale_after)
    except ValueError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 2

    def stop(signum, _frame):
        log.info("received signal %d, shutting down", signum)
        threading.Thread(target=srv.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    host, port = srv.server_address[:2]
    log.info("dashboard on http://%s:%d/ reading %s", host if ":" not in host else "[%s]" % host,
             port, os.path.abspath(args.data_dir))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
