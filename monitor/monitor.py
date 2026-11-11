#!/usr/bin/env python3
"""Monitor for the Linux Self-Healing Lab.

Every --interval seconds it:
  1. asks systemd about the unit (`systemctl show`: state, PID, restart
     counter, cgroup CPU/memory, exit info)
  2. calls the service's /health endpoint and checks that the PID it reports
     matches systemd's MainPID
  3. feeds the result into an IncidentTracker, which opens/closes incidents
  4. writes <data-dir>/status.json (snapshot + short history), mode 0640
  5. appends every closed incident to <data-dir>/incidents.jsonl, mode 0640
     (group-readable so the dashboard, and only it, can read them)

Timing (details in docs/MONITOR.md, "How measurement works"):
  * failed_at    - systemd's ExecMainExitTimestampMonotonic: when systemd
                   recorded the main process exit (microsecond resolution;
                   recorded when systemd handled the event, so it can lag the
                   actual death slightly).
  * restarted_at - systemd's ExecMainStartTimestampMonotonic of the new PID.
  * healthy_at   - known only to lie in a window: after the start of the last
                   poll that saw the service unhealthy, and no later than the
                   end of the first poll that saw it healthy. Both ends are recorded, so each incident
                   carries its own measured uncertainty; nothing is assumed
                   about poll regularity.
  All values use CLOCK_MONOTONIC (the clock systemd uses for *Monotonic
  properties), so they can be subtracted directly.

Standard library only. Python 3.8+.
"""

import argparse
import collections
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("monitor")

PROPERTIES = [
    "LoadState", "ActiveState", "SubState", "Result", "MainPID", "NRestarts",
    "MemoryCurrent", "MemoryMax", "CPUUsageNSec",
    "ExecMainStartTimestampMonotonic", "ExecMainExitTimestampMonotonic",
    "ExecMainCode", "ExecMainStatus",
]

UINT64_MAX = 2 ** 64 - 1  # systemd uses this to mean "not set"
AUTO_RESTART_SUBSTATES = ("auto-restart", "auto-restart-queued")
TRANSITIONAL_STATES = ("activating", "deactivating", "reloading")


# --------------------------------------------------------------------------
# Parsing helpers
# --------------------------------------------------------------------------

def parse_show(text: str) -> Dict[str, str]:
    """Parse `systemctl show` KEY=VALUE output into a dict."""
    props = {}
    for line in text.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            props[key] = value
    return props


def to_int(value: Optional[str]) -> Optional[int]:
    """Convert a systemd property to int; None for '', '[not set]', 'infinity'."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return None if n == UINT64_MAX else n


def mono_us_to_s(value: Optional[str]) -> Optional[float]:
    """systemd monotonic timestamps are microseconds; 0 means 'never'."""
    n = to_int(value)
    return n / 1e6 if n else None


def describe_exit(code: Optional[int], status: Optional[int]) -> Optional[str]:
    """ExecMainCode follows waitid(): 1=exited, 2=killed, 3=dumped core."""
    if code == 1:
        return "exit code %s" % status
    if code in (2, 3):
        try:
            name = signal.Signals(status).name
        except (ValueError, TypeError):
            name = "?"
        return "signal %s (%s)%s" % (status, name, " + core dump" if code == 3 else "")
    return None


def _r(x: Optional[float]) -> Optional[float]:
    return round(x, 3) if x is not None else None


# --------------------------------------------------------------------------
# Collecting a sample
# --------------------------------------------------------------------------

@dataclass
class Sample:
    mono: float                         # CLOCK_MONOTONIC when the sample was complete
    poll_start_mono: float              # CLOCK_MONOTONIC when the sample started
    wall: float                         # epoch seconds, for display only
    systemd_ok: bool                    # False -> systemd fields below are not valid
    systemd_error: Optional[str]
    active: str = "unknown"             # ActiveState
    sub: str = "unknown"                # SubState
    result: str = "unknown"             # Result
    pid: int = 0                        # MainPID (0 = no process)
    nrestarts: int = 0                  # automatic restarts counted by systemd
    mem_bytes: Optional[int] = None
    mem_max: Optional[int] = None
    cpu_nsec: Optional[int] = None
    start_mono: Optional[float] = None  # ExecMainStartTimestampMonotonic
    exit_mono: Optional[float] = None   # ExecMainExitTimestampMonotonic
    exit_code: Optional[int] = None
    exit_status: Optional[int] = None
    health_ok: bool = False             # HTTP OK *and* PID matches systemd
    health_pid: Optional[int] = None
    health_error: Optional[str] = None
    health_start_mono: Optional[float] = None
    health_end_mono: Optional[float] = None
    systemctl_ms: Optional[float] = None
    health_ms: Optional[float] = None
    cpu_pct: Optional[float] = None     # filled in by the main loop

    @property
    def healthy(self) -> bool:
        return self.systemd_ok and self.active == "active" and self.health_ok


def systemctl_show(unit: str, timeout: float) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Return (properties, None) or (None, error message). Never raises."""
    cmd = ["systemctl", "show", unit, "--property=" + ",".join(PROPERTIES)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "systemctl not found"
    except subprocess.TimeoutExpired:
        return None, "systemctl timed out after %.1fs" % timeout
    except OSError as exc:
        return None, "systemctl could not run: %s" % exc
    if out.returncode != 0:
        err = (out.stderr.strip().splitlines() or ["no error output"])[0]
        return None, "systemctl exited %d: %s" % (out.returncode, err)
    props = parse_show(out.stdout)
    missing = [p for p in PROPERTIES if p not in props]
    if missing:
        return None, "systemctl output missing properties: %s" % ", ".join(missing)
    if props["LoadState"] != "loaded":
        return None, "unit LoadState=%s" % props["LoadState"]
    return props, None


def check_health(url: str, timeout: float):
    """Return (ok, pid, error, start_mono, end_mono)."""
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
        pid = body.get("pid") if isinstance(body.get("pid"), int) else None
        ok = body.get("status") == "ok"
        return ok, pid, None if ok else "bad response", t0, time.monotonic()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, None, str(getattr(exc, "reason", exc)), t0, time.monotonic()


def _fill_systemd(s: Sample, p: Dict[str, str]):
    s.active, s.sub, s.result = p["ActiveState"], p["SubState"], p["Result"]
    s.pid = to_int(p["MainPID"]) or 0
    s.nrestarts = to_int(p["NRestarts"]) or 0
    s.mem_bytes, s.mem_max = to_int(p["MemoryCurrent"]), to_int(p["MemoryMax"])
    s.cpu_nsec = to_int(p["CPUUsageNSec"])
    s.start_mono = mono_us_to_s(p["ExecMainStartTimestampMonotonic"])
    s.exit_mono = mono_us_to_s(p["ExecMainExitTimestampMonotonic"])
    s.exit_code, s.exit_status = to_int(p["ExecMainCode"]), to_int(p["ExecMainStatus"])


def collect_sample(unit: str, health_url: str, health_timeout: float,
                   systemctl_timeout: float, show=systemctl_show, health=check_health) -> Sample:
    t0 = time.monotonic()
    props, err = show(unit, systemctl_timeout)
    t1 = time.monotonic()
    ok, hpid, herr, hstart, hend = health(health_url, health_timeout)

    # systemd and /health are queried one after the other. If the PIDs differ,
    # the process may have been replaced in between: ask systemd once more.
    if props is not None and ok and hpid != (to_int(props["MainPID"]) or 0):
        props2, err2 = show(unit, systemctl_timeout)
        if props2 is not None and (to_int(props2["MainPID"]) or 0) == hpid:
            props = props2
        else:
            ok = False
            herr = "PID mismatch: /health reports pid=%s but systemd MainPID=%s" % (
                hpid, props["MainPID"] if props2 is None else props2["MainPID"])
            if props2 is not None:
                props = props2
            elif err2:
                props, err = None, err2

    s = Sample(mono=time.monotonic(), poll_start_mono=t0, wall=time.time(),
               systemd_ok=props is not None,
               systemd_error=err, health_ok=ok, health_pid=hpid, health_error=herr,
               health_start_mono=hstart, health_end_mono=hend,
               systemctl_ms=round((t1 - t0) * 1000, 1),
               health_ms=round((hend - hstart) * 1000, 1))
    if props is not None:
        _fill_systemd(s, props)
    return s


def cpu_percent(prev: Optional[Sample], cur: Sample) -> Optional[float]:
    """CPU% of one core = CPU time used / wall time elapsed, between two samples."""
    if not prev or prev.cpu_nsec is None or cur.cpu_nsec is None:
        return None
    d_cpu = cur.cpu_nsec - prev.cpu_nsec
    d_wall = cur.mono - prev.mono
    if d_cpu < 0 or d_wall <= 0:  # counter reset (new cgroup) or clock oddity
        return None
    return round(d_cpu / 1e9 / d_wall * 100, 1)


# --------------------------------------------------------------------------
# Incident detection
# --------------------------------------------------------------------------

def failure_evident(s: Sample) -> bool:
    """systemd itself reports a failure (not just a slow or planned transition)."""
    return (s.active == "failed" or s.sub in AUTO_RESTART_SUBSTATES
            or (s.active == "inactive" and s.result != "success"))


class IncidentTracker:
    """State machine over samples. Pure logic (no I/O) so it is unit-testable.

    Every closed record has a `classification`:
      failure    - systemd reported a failure, its automatic-restart counter
                   increased, or the service stopped answering /health
      uncertain  - the process was replaced but there is no failure evidence;
                   most likely a manual restart, which the monitor cannot prove
    Planned stops/restarts with clear evidence (clean stop seen: inactive +
    Result=success) are not incidents; they go to `planned_events`.
    """

    def __init__(self, fail_threshold: int = 2, transition_grace: float = 15.0,
                 next_id: int = 1):
        self.fail_threshold = fail_threshold
        self.transition_grace = transition_grace
        self.next_id = next_id
        self.incident: Optional[dict] = None
        self.last_healthy: Optional[Sample] = None
        self.prev: Optional[Sample] = None
        self.status = "unknown"
        self.planned_events = collections.deque(maxlen=20)
        self.monitor_errors = 0
        self._reset_pending()

    def _reset_pending(self):
        self.consecutive_fail = 0
        self.first_fail: Optional[Sample] = None
        self.transition_since: Optional[float] = None
        self.observed_states: List[str] = []
        self.clean_stop_seen = False

    # ------------------------------------------------------------------
    def update(self, s: Sample) -> List[dict]:
        events: List[dict] = []
        gap = s.mono - self.prev.mono if self.prev else None
        self.prev = s

        if not s.systemd_ok:
            # No trustworthy systemd data: do not open or close anything.
            self.monitor_errors += 1
            self.status = "monitor-error"
            if self.incident:
                self.incident["monitor_errors"] += 1
            return events

        if self.incident is not None:
            self._track_open(s, gap)
            if s.healthy:
                events.append({"type": "close", "incident": self._close(s)})
                self._mark_healthy(s)
            else:
                self.status = "failed" if s.active == "failed" else "down"
            return events

        state = "%s/%s" % (s.active, s.sub)
        if not s.healthy and state not in self.observed_states:
            self.observed_states.append(state)

        if s.healthy:
            lh = self.last_healthy
            if lh and s.pid != lh.pid:
                events.extend(self._replaced_between_polls(s, lh))
            self._mark_healthy(s)
        elif s.active == "inactive" and s.result == "success":
            self.clean_stop_seen = True          # planned stop: not an incident
            self.status = "stopped"
        elif failure_evident(s):
            events.append(self._open(s, "process-failure"))
        elif s.active in TRANSITIONAL_STATES:
            # e.g. a manual `systemctl restart` in progress. Only an incident
            # if it takes unusually long.
            if self.transition_since is None:
                self.transition_since = s.mono
            self.status = "transitioning"
            if s.mono - self.transition_since >= self.transition_grace:
                events.append(self._open(s, "stuck-transition"))
        else:
            # active, but /health failed or reported the wrong PID
            self.consecutive_fail += 1
            if self.first_fail is None:
                self.first_fail = s
            if self.consecutive_fail >= self.fail_threshold:
                events.append(self._open(s, "unhealthy-but-running"))
            else:
                self.status = "degraded"
        return events

    # ------------------------------------------------------------------
    def _mark_healthy(self, s: Sample):
        self.status = "healthy"
        self.last_healthy = s
        self._reset_pending()

    def _replaced_between_polls(self, s: Sample, lh: Sample) -> List[dict]:
        """PID changed and we never saw a failure state."""
        counter_up = s.nrestarts > lh.nrestarts
        if self.clean_stop_seen and not counter_up:
            ev = {"t": iso(s.wall), "type": "planned-restart", "old_pid": lh.pid,
                  "new_pid": s.pid, "evidence": "clean stop observed (inactive, "
                  "Result=success); automatic-restart counter did not increase"}
            self.planned_events.append(ev)
            return [{"type": "planned", "event": ev}]
        if counter_up:
            classification, kind = "failure", "process-failure"
            cause = "unknown (exit happened between polls)"
            evidence = "NRestarts %d -> %d: systemd performed an automatic restart" % (
                lh.nrestarts, s.nrestarts)
        else:
            classification, kind = "uncertain", "restart-unclassified"
            cause = "uncertain: process replaced without an automatic-restart count; " \
                    "most likely a manual restart, not confirmed"
            evidence = "NRestarts %d -> %d, observed states: %s" % (
                lh.nrestarts, s.nrestarts, ", ".join(self.observed_states) or "none")
        opened = self._open(s, kind, last_ok=lh, missed=True)
        self.incident.update(classification=classification, cause=cause, evidence=evidence)
        opened["incident"] = dict(self.incident)
        return [opened, {"type": "close", "incident": self._close(s)}]

    def _open(self, s: Sample, kind: str, last_ok: Optional[Sample] = None,
              missed: bool = False) -> dict:
        last_ok = last_ok or self.last_healthy
        first_fail = None if missed else (self.first_fail or s)
        self.incident = {
            "id": self.next_id,
            "classification": "failure",
            "kind": kind,
            "cause": None,
            "evidence": "systemd state %s/%s, Result=%s" % (s.active, s.sub, s.result)
                        if kind != "unhealthy-but-running" else
                        "/health failed %d consecutive polls: %s" % (
                            self.consecutive_fail, s.health_error),
            "systemd_result": None,
            "old_pid": last_ok.pid if last_ok else None,
            "old_nrestarts": last_ok.nrestarts if last_ok else None,
            "last_ok_mono": last_ok.mono if last_ok else None,
            "last_ok_poll_start_mono": last_ok.poll_start_mono if last_ok else None,
            "first_fail_poll_start_mono": first_fail.poll_start_mono if first_fail else None,
            "last_fail_poll_start_mono": None if missed else s.poll_start_mono,
            "detected_mono": s.mono,
            "failed_at_mono": None,
            "failed_at_source": None,
            "gave_up": False,
            "missed_down_window": missed,
            "max_poll_gap_s": None,
            "monitor_errors": 0,
            "health_error": s.health_error,
        }
        self.next_id += 1
        self.status = "down"
        self._capture_failure(s)
        return {"type": "open", "incident": dict(self.incident)}

    def _track_open(self, s: Sample, gap: Optional[float]):
        inc = self.incident
        self._capture_failure(s)
        if s.active == "failed":
            inc["gave_up"] = True  # StartLimitBurst reached
        if gap is not None:
            inc["max_poll_gap_s"] = _r(max(gap, inc["max_poll_gap_s"] or 0))
        if not s.healthy:
            inc["last_fail_poll_start_mono"] = s.poll_start_mono

    def _capture_failure(self, s: Sample):
        """While the process is gone, systemd still holds its exit time and cause.

        systemd resets these fields when the replacement process starts, so
        they are only available if a poll lands inside the restart window.
        """
        inc = self.incident
        if inc["failed_at_mono"] is None and s.pid == 0 and s.exit_mono:
            inc["failed_at_mono"] = s.exit_mono
            inc["failed_at_source"] = "systemd ExecMainExitTimestampMonotonic"
            inc["cause"] = describe_exit(s.exit_code, s.exit_status)
            inc["systemd_result"] = s.result

    def _close(self, s: Sample) -> dict:
        inc = self.incident
        self.incident = None
        if inc["failed_at_mono"] is None and inc["kind"] == "unhealthy-but-running" \
                and inc["first_fail_poll_start_mono"] is not None:
            # A hang has no process-exit timestamp. The monitor only knows when
            # it FIRST SAW the problem; the hang itself may have started earlier.
            # Timings below are therefore measured from detection, not from the
            # start of the hang, and are not bounds on the full outage.
            inc["failed_at_mono"] = inc["first_fail_poll_start_mono"]
            inc["failed_at_source"] = "estimate: start of first unhealthy poll (monitor)"

        if inc["failed_at_source"] and inc["failed_at_source"].startswith("systemd"):
            basis = "systemd-exit-timestamp"
        elif inc["failed_at_source"]:
            basis = "first-unhealthy-poll"   # problem start unknown; measured from detection
        else:
            basis = "unknown"                # no failure time: only an upper bound is given

        replaced = s.pid != inc["old_pid"]
        delta = (s.nrestarts - inc["old_nrestarts"]) if inc["old_nrestarts"] is not None else None
        if not replaced:
            recovered_by = "same process recovered (no restart)"
        elif delta is not None and delta > 0:
            recovered_by = "systemd automatic restart (Restart=on-failure)"
        else:
            recovered_by = "restart not counted as automatic (likely manual start)"

        t_fail = inc["failed_at_mono"]
        t_restart = s.start_mono if replaced else None
        upper_end = s.mono                            # first healthy poll finished
        lower_end = inc["last_fail_poll_start_mono"]  # last unhealthy poll started
        if t_restart and (lower_end is None or t_restart > lower_end):
            # It cannot have been healthy before the new process existed.
            lower_end = t_restart

        inc.update({
            "new_pid": s.pid,
            "process_replaced": replaced,
            "recovered_by": recovered_by,
            # None if the counter went backwards (reset by a manual start)
            "restarts_during_incident": delta if (delta is not None and delta >= 0) else None,
            "restarted_at_mono": t_restart,
            "healthy_at_mono": upper_end,
            "timing_basis": basis,
            # Detection time is only meaningful when the failure time is known.
            "time_to_detect_s": _r(inc["detected_mono"] - t_fail)
                                if (t_fail and basis == "systemd-exit-timestamp") else None,
            "restart_delay_s": _r(t_restart - t_fail) if (t_fail and t_restart) else None,
            # With basis systemd-exit-timestamp: recovery lies in
            # [lower_bound, recovery_time_s], measured from the recorded exit.
            # With basis first-unhealthy-poll: same window, but measured from
            # detection; the outage may have begun earlier (see next field).
            "recovery_time_s": _r(upper_end - t_fail) if t_fail else None,
            "recovery_time_lower_bound_s": _r(lower_end - t_fail)
                                           if (t_fail and lower_end) else None,
            # Hang only: time between the last poll that saw the service healthy
            # and the first that saw it unhealthy. The hang most likely began in
            # this gap, but the monitor cannot prove that (a check can pass while
            # the service is already degrading), so this is not a guarantee.
            "undetected_window_before_estimate_s":
                _r(inc["first_fail_poll_start_mono"] - inc["last_ok_mono"])
                if (basis == "first-unhealthy-poll" and inc["last_ok_mono"]) else None,
            # From the start of the last healthy poll to the end of the first
            # healthy one again (as judged by the monitor's own checks).
            "recovery_upper_bound_from_last_ok_s":
                _r(upper_end - inc["last_ok_poll_start_mono"])
                if inc["last_ok_poll_start_mono"] else None,
        })
        return inc


# --------------------------------------------------------------------------
# Output and logs
# --------------------------------------------------------------------------

def iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="milliseconds")


def add_wall_times(inc: dict) -> dict:
    """Add human-readable local timestamps next to each *_mono field."""
    offset = time.time() - time.monotonic()
    out = dict(inc)
    for key, value in inc.items():
        if key.endswith("_mono"):
            out[key[:-5]] = iso(value + offset) if value is not None else None
    return out


def read_app_log(path: str, since: Optional[float] = None, until: Optional[float] = None,
                 max_lines: int = 30, tail_bytes: int = 65536) -> List[str]:
    """Last lines of the demo's own log file (and its first rotation), optionally
    filtered to a wall-clock window. Lines start with an ISO-8601 timestamp."""
    lines: List[str] = []
    for p in (path + ".1", path):
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                f.seek(max(0, f.tell() - tail_bytes))
                chunk = f.read().decode("utf-8", "replace").splitlines()
                lines.extend(chunk[1:] if f.tell() > tail_bytes else chunk)
        except FileNotFoundError:
            continue
        except OSError as exc:
            return ["<cannot read %s: %s>" % (p, exc)]
    if since is not None or until is not None:
        kept = []
        for line in lines:
            try:
                t = datetime.fromisoformat(line.split(" ", 1)[0]).timestamp()
            except ValueError:
                continue
            if (since is None or t >= since) and (until is None or t <= until):
                kept.append(line)
        lines = kept
    return lines[-max_lines:]


DATA_FILE_MODE = 0o640  # owner (monitor) rw, group (dashboard) r, others nothing


def write_data(path: str, text: str):
    """Atomic write of a data file with mode 0640.

    The temp file is always created fresh (O_EXCL, never through a symlink) so
    it gets the directory's group (setgid dir -> group shlab-dash) and our mode,
    instead of inheriting whatever an old leftover temp file had.
    """
    tmp = path + ".tmp"
    if os.path.lexists(tmp):
        os.unlink(tmp)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, DATA_FILE_MODE)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, DATA_FILE_MODE)
    os.replace(tmp, path)


def append_data(path: str, line: str):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, DATA_FILE_MODE)
    with os.fdopen(fd, "a") as f:
        f.write(line + "\n")


def count_lines(path: str) -> int:
    try:
        with open(path) as f:
            return sum(1 for _ in f)
    except FileNotFoundError:
        return 0


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Self-Healing Lab monitor")
    ap.add_argument("--unit", default="demo-service.service")
    ap.add_argument("--health-url", default="http://127.0.0.1:8080/health")
    ap.add_argument("--interval", type=float, default=1.0, help="target poll interval (s)")
    ap.add_argument("--timeout", type=float, default=0.8, help="health check timeout (s)")
    ap.add_argument("--systemctl-timeout", type=float, default=3.0)
    ap.add_argument("--fail-threshold", type=int, default=2,
                    help="consecutive failed health checks (while systemd says 'active') "
                         "before opening an incident")
    ap.add_argument("--transition-grace", type=float, default=15.0,
                    help="seconds a start/stop may take before it counts as an incident")
    ap.add_argument("--data-dir", default="./data")
    ap.add_argument("--app-log", default=None, help="demo service log file to read")
    ap.add_argument("--history", type=int, default=300, help="samples kept in status.json")
    args = ap.parse_args(argv)

    os.umask(0o027)
    logging.basicConfig(stream=sys.stdout, level=logging.INFO,
                        format="%(levelname)s %(message)s")
    os.makedirs(args.data_dir, mode=0o750, exist_ok=True)
    status_path = os.path.join(args.data_dir, "status.json")
    incidents_path = os.path.join(args.data_dir, "incidents.jsonl")

    tracker = IncidentTracker(args.fail_threshold, args.transition_grace,
                              next_id=count_lines(incidents_path) + 1)
    history = collections.deque(maxlen=args.history)
    recent_logs: List[str] = []
    prev: Optional[Sample] = None
    last_error: Optional[str] = None
    tick = 0
    log.info("monitoring %s every %.1fs, data in %s", args.unit, args.interval,
             os.path.abspath(args.data_dir))

    while True:
        t_start = time.monotonic()
        s = collect_sample(args.unit, args.health_url, args.timeout, args.systemctl_timeout)
        s.cpu_pct = cpu_percent(prev, s) if s.systemd_ok else None
        gap = s.mono - prev.mono if prev else None
        prev = s if s.systemd_ok else prev

        if s.systemd_error != last_error:  # log errors on change, not every poll
            if s.systemd_error:
                log.error("systemd query failed: %s", s.systemd_error)
            else:
                log.info("systemd query OK again")
            last_error = s.systemd_error

        for ev in tracker.update(s):
            if ev["type"] == "planned":
                log.info("planned restart: pid %s -> %s", ev["event"]["old_pid"],
                         ev["event"]["new_pid"])
                continue
            inc = ev["incident"]
            if ev["type"] == "open":
                log.warning("INCIDENT #%d OPEN class=%s kind=%s state=%s/%s pid=%s error=%s",
                            inc["id"], inc["classification"], inc["kind"], s.active, s.sub,
                            inc["old_pid"], inc["health_error"])
                continue
            if args.app_log:
                offset = time.time() - time.monotonic()
                start = inc["failed_at_mono"] or inc["last_ok_mono"] or s.mono
                inc["logs"] = read_app_log(args.app_log, since=start + offset - 5,
                                           until=s.wall + 1)
            append_data(incidents_path, json.dumps(add_wall_times(inc)))
            log.warning("INCIDENT #%d CLOSED class=%s cause=%s recovery=%s..%ss "
                        "(measured from: %s) restart_delay=%ss recovered_by=%s pid %s -> %s",
                        inc["id"], inc["classification"], inc["cause"],
                        inc["recovery_time_lower_bound_s"], inc["recovery_time_s"],
                        inc["timing_basis"],
                        inc["restart_delay_s"], inc["recovered_by"],
                        inc["old_pid"], inc["new_pid"])

        if args.app_log and tick % 5 == 0:  # refresh the log tail every 5 polls
            recent_logs = read_app_log(args.app_log, max_lines=15)
        tick += 1

        history.append({"t": iso(s.wall), "healthy": s.healthy, "cpu_pct": s.cpu_pct,
                        "mem_bytes": s.mem_bytes, "health_ms": s.health_ms,
                        "systemctl_ms": s.systemctl_ms, "poll_gap_s": _r(gap)})
        write_data(status_path, json.dumps({
            "updated": iso(s.wall),
            "unit": args.unit,
            "status": tracker.status,
            "current": asdict(s),
            "open_incident": add_wall_times(tracker.incident) if tracker.incident else None,
            "planned_events": list(tracker.planned_events),
            "monitor_errors_total": tracker.monitor_errors,
            "history": list(history),
            "recent_logs": recent_logs,
        }, indent=2))

        time.sleep(max(0.0, args.interval - (time.monotonic() - t_start)))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
