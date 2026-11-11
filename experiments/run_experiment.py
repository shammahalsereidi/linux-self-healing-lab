#!/usr/bin/env python3
"""Repeatable, controlled failure experiments for the Linux Self-Healing Lab.

Scenarios (each acts ONLY on demo-service.service):
  kill   systemd sends SIGKILL to the service's main process
         -> expected: failure, cause "signal 9 (SIGKILL)", automatic restart
  crash  POST http://127.0.0.1:8080/crash (the app exits with code 1)
         -> expected: failure, cause "exit code 1", automatic restart
  hang   SIGSTOP the main process for N seconds, then SIGCONT
         -> expected: "unhealthy-but-running", same PID, no restart
            (systemd does not restart a stopped-but-alive process)

Safety:
  * The target unit is a constant. Signals are sent with
    `systemctl kill --kill-whom=main`, so systemd resolves the PID itself
    (no risk of signalling a reused PID).
  * Runs as your normal user. Only the `systemctl kill` call uses sudo.
  * Results are read through the dashboard's read-only API
    (http://127.0.0.1:8081/api/...). This tool never opens the monitor's
    files, so it cannot change or delete existing incident records.
  * One experiment at a time on this machine: a system-wide lock (an
    abstract Unix socket), independent of --out-dir.
  * Crash-loop limit: the unit allows StartLimitBurst=5 starts per 60 s.
    The script waits until the service has been up >= 20 s before each
    injection, so its OWN injections are >= 20 s apart. That does NOT
    guarantee the limit is never reached: systemd does not expose its
    start-limit counter, and starts by anything else (manual restarts,
    crashes, other tools) also count. If the limit is hit, the script stops
    and prints how to recover.
  * Small: 3 runs by default, at most 5 per invocation. Stops at the first
    unexpected result.
  * Ctrl+C / SIGTERM stop the experiment. If a hang is in progress, the
    script sends SIGCONT to the unit's processes and then checks that the
    SAME process (PID + start time) is running again (state R/S/D, answering
    /health) and that no process of the unit is still stopped. A zombie or a
    reused PID never counts as resumed. If the main PID changed during the
    hang, the run is discarded. If it cannot confirm, it says so and prints
    the command to run.

Usage:
  python3 experiments/run_experiment.py kill  [--runs 3]
  python3 experiments/run_experiment.py crash [--runs 3]
  python3 experiments/run_experiment.py hang  [--runs 3] [--hang-seconds 5]
  python3 experiments/run_experiment.py kill --dry-run   # checks only, no injection

Exit codes: 0 all runs as expected; 1 unexpected result, error, stopped,
or main PID changed during a hang;
3 systemd start limit hit; 4 a hang could not be confirmed as resumed.

Standard library only. Python 3.8+.
"""

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

UNIT = "demo-service.service"          # the only unit this tool ever acts on
MONITOR_UNIT = "demo-monitor.service"
HEALTH_URL = "http://127.0.0.1:8080/health"
CRASH_URL = "http://127.0.0.1:8080/crash"
DASHBOARD = "http://127.0.0.1:8081"
LOCK_NAME = "self-healing-lab-experiment"   # abstract socket name (system-wide)

START_LIMIT_BURST = 5                  # from systemd/demo-service.service
START_LIMIT_INTERVAL_S = 60
MIN_UPTIME_S = 20.0                    # spacing of this script's own injections
MAX_RUNS = 5
HANG_MIN_S, HANG_MAX_S = 3.0, 30.0
READY_TIMEOUT_S = 120.0
INCIDENT_TIMEOUT_S = 60.0              # kill/crash; hang adds its duration
COMMAND_TIMEOUT_S = 30.0
RESUME_CONFIRM_S = 5.0
STOPPED_STATES = ("T", "t")          # stopped / traced
RUNNING_STATES = ("R", "S", "D")     # running, sleeping, disk wait
EXITED_STATES = ("Z", "X")           # zombie (exited, not yet reaped), dead

RESUME_HELP = ("sudo systemctl kill --kill-whom=all --signal=SIGCONT %s\n"
               "Then check: curl -s http://127.0.0.1:8080/health" % UNIT)
START_LIMIT_HELP = ("systemd stopped restarting %s because it was started more than %d "
                    "times within %d s (Result=start-limit-hit). This can include starts "
                    "that were not made by this script. To recover:\n"
                    "  sudo systemctl reset-failed %s\n  sudo systemctl start %s"
                    % (UNIT, START_LIMIT_BURST, START_LIMIT_INTERVAL_S, UNIT, UNIT))

EXPECT = {
    "kill": {"classification": "failure", "kind": "process-failure",
             "cause": "signal 9 (SIGKILL)", "timing_basis": "systemd-exit-timestamp",
             "pid_changes": True, "recovered_by_contains": "automatic restart"},
    "crash": {"classification": "failure", "kind": "process-failure",
              "cause": "exit code 1", "timing_basis": "systemd-exit-timestamp",
              "pid_changes": True, "recovered_by_contains": "automatic restart"},
    "hang": {"classification": "failure", "kind": "unhealthy-but-running",
             "timing_basis": "first-unhealthy-poll", "pid_changes": False,
             "recovered_by_contains": "same process"},
}

# Where client_recovery_s starts, per scenario (also stored in each record).
CLIENT_START = {
    "kill": "before the sudo systemctl kill command is started (includes its latency)",
    "crash": "before the POST /crash request is sent",
    "hang": "after the SIGCONT command returned",
}

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(REPO, "experiments", "results")


class ExperimentError(Exception):
    pass


class StartLimitHit(ExperimentError):
    pass


class ResumeNotConfirmed(ExperimentError):
    pass


class PidChangedDuringHang(ExperimentError):
    """The service's main process changed while a hang was being injected:
    the run does not measure what it is meant to and is discarded."""


class Stop(Exception):
    """Raised by SIGINT/SIGTERM/SIGHUP handlers to stop cleanly."""


# --------------------------------------------------------------------------
# Lock: one experiment per machine, whatever --out-dir is used
# --------------------------------------------------------------------------

def acquire_lock(name: str = LOCK_NAME) -> socket.socket:
    """Bind an abstract Unix socket (Linux). Only one process can hold the name;
    the kernel releases it when the process exits, even on a crash or SIGKILL,
    so there is no stale lock file to clean up."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.bind("\0" + name)
    except OSError:
        s.close()
        raise ExperimentError("another experiment is already running on this machine")
    return s


# --------------------------------------------------------------------------
# Lab: every interaction with the system. Tests substitute a fake.
# --------------------------------------------------------------------------

def kill_whom_flag(version_line: str) -> str:
    """systemd >= 252 calls it --kill-whom; older versions --kill-who."""
    try:
        version = int(version_line.split()[1])
    except (IndexError, ValueError):
        return "--kill-whom"
    return "--kill-whom" if version >= 252 else "--kill-who"


def kill_command(sig: str, flag: str = "--kill-whom") -> List[str]:
    """SIGKILL/SIGSTOP: main process only. SIGCONT: every process of the unit,
    so a stopped process is resumed even if it is no longer the main one
    (SIGCONT has no effect on a process that is not stopped)."""
    whom = {"SIGKILL": "main", "SIGSTOP": "main", "SIGCONT": "all"}.get(sig)
    if whom is None:
        raise ValueError("unsupported signal %s" % sig)
    return ["systemctl", "kill", "%s=%s" % (flag, whom), "--signal=" + sig, UNIT]


def _read(path: str) -> Optional[str]:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def parse_proc_stat(text: Optional[str]) -> Optional[Tuple[str, int]]:
    """Parse /proc/<pid>/stat. The command name (field 2) is in parentheses and may
    contain spaces or ')', so split after the LAST ')'. Then field 3 is the state
    and field 22 the start time."""
    if not text or ")" not in text:
        return None
    rest = text[text.rindex(")") + 2:].split()
    try:
        return rest[0], int(rest[19])
    except (IndexError, ValueError):
        return None


class Lab:
    def __init__(self):
        self.sudo = [] if os.geteuid() == 0 else ["sudo"]
        try:
            out = subprocess.run(["systemctl", "--version"], capture_output=True,
                                 text=True, timeout=5).stdout
            self.flag = kill_whom_flag(out.splitlines()[0] if out else "")
        except (OSError, subprocess.TimeoutExpired):
            self.flag = "--kill-whom"

    def now(self) -> float:
        return time.monotonic()  # same clock as systemd's *Monotonic properties

    def sleep(self, seconds: float):
        time.sleep(seconds)

    def show(self, unit: str, props: List[str]) -> Dict[str, str]:
        try:
            res = subprocess.run(["systemctl", "show", unit, "--property=" + ",".join(props)],
                                 capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ExperimentError("systemctl show %s failed: %s" % (unit, exc))
        if res.returncode != 0:
            raise ExperimentError("systemctl show %s failed: %s" % (unit, res.stderr.strip()))
        return dict(l.split("=", 1) for l in res.stdout.splitlines() if "=" in l)

    def health(self) -> Tuple[bool, Optional[int]]:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=0.5) as r:
                body = json.loads(r.read().decode() or "{}")
            return body.get("status") == "ok", body.get("pid")
        except (urllib.error.URLError, OSError, ValueError):
            return False, None

    def process_info(self, pid: int) -> Optional[Tuple[str, int]]:
        """(state letter, start time in clock ticks since boot) from /proc/<pid>/stat,
        or None if there is no such process. The start time identifies the process:
        a reused PID has a different start time."""
        return parse_proc_stat(_read("/proc/%d/stat" % pid)) if pid > 0 else None

    def unit_pids(self) -> Optional[List[int]]:
        """All PIDs in the unit's cgroup (cgroup v2), or None if not readable."""
        try:
            cg = self.show(UNIT, ["ControlGroup"]).get("ControlGroup")
        except ExperimentError:
            return None
        text = _read("/sys/fs/cgroup%s/cgroup.procs" % cg) if cg else None
        if text is None:
            return None
        return [int(x) for x in text.split() if x.isdigit()]

    def dashboard(self, path: str) -> dict:
        try:
            with urllib.request.urlopen(DASHBOARD + path, timeout=3) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ExperimentError("dashboard API %s not reachable (%s). Is demo-dashboard "
                                  "running?" % (path, exc))

    def validate_sudo(self):
        if self.sudo and subprocess.run(["sudo", "-v"]).returncode != 0:
            raise ExperimentError("sudo authentication failed")

    def signal_main(self, sig: str, interactive: bool = False):
        """Raise ExperimentError on ANY failure: non-zero exit, timeout, missing binary."""
        # Injections use `sudo -n` (never block on a password prompt mid-run).
        # SIGCONT may prompt: resuming a stopped service must not be skipped.
        prefix = self.sudo + ([] if interactive or not self.sudo else ["-n"])
        cmd = prefix + kill_command(sig, self.flag)
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ExperimentError("%s timed out after %ds" % (" ".join(cmd), COMMAND_TIMEOUT_S))
        except OSError as exc:
            raise ExperimentError("%s could not run: %s" % (" ".join(cmd), exc))
        if res.returncode != 0:
            raise ExperimentError("%s failed (exit %d): %s"
                                  % (" ".join(cmd), res.returncode, res.stderr.strip()))

    def crash(self):
        req = urllib.request.Request(CRASH_URL, data=b"", method="POST")
        try:
            urllib.request.urlopen(req, timeout=2).read()
        except (urllib.error.URLError, OSError):
            pass  # the process exits right after answering; a reset is fine


# --------------------------------------------------------------------------
# Experiment
# --------------------------------------------------------------------------

def check_expectations(scenario: str, inc: dict, old_pid: int) -> Dict[str, bool]:
    exp = EXPECT[scenario]
    checks = {
        "classification": inc.get("classification") == exp["classification"],
        "kind": inc.get("kind") == exp["kind"],
        "timing_basis": inc.get("timing_basis") == exp["timing_basis"],
        "old_pid_matches": inc.get("old_pid") == old_pid,
        "pid_changed" if exp["pid_changes"] else "pid_unchanged":
            (inc.get("new_pid") != old_pid) if exp["pid_changes"]
            else (inc.get("new_pid") == old_pid),
        "recovered_by": exp["recovered_by_contains"] in (inc.get("recovered_by") or ""),
    }
    if "cause" in exp:
        checks["cause"] = inc.get("cause") == exp["cause"]
    return checks


class Experiment:
    def __init__(self, lab, scenario: str, runs: int, hang_s: float, out_dir: str,
                 log=print, warn=None):
        self.lab, self.scenario, self.runs, self.hang_s = lab, scenario, runs, hang_s
        self.out_dir, self.log = out_dir, log
        self.warn = warn or (lambda msg: print(msg, file=sys.stderr))
        self.stopped_service = False   # True while the demo may be SIGSTOPped
        self.hang_pid: Optional[int] = None
        self.hang_start: Optional[int] = None   # /proc start time of the stopped process
        self.resume_attempts = 0
        self.last_resume_status: Optional[str] = None
        self.last_resume_seen = ""
        self.records: List[dict] = []
        self.out_path: Optional[str] = None

    # -- checks ----------------------------------------------------------
    def validate_args(self) -> List[str]:
        problems = []
        if self.scenario not in EXPECT:
            problems.append("unknown scenario %r" % self.scenario)
        if not 1 <= self.runs <= MAX_RUNS:
            problems.append("--runs must be between 1 and %d" % MAX_RUNS)
        if self.scenario == "hang" and not HANG_MIN_S <= self.hang_s <= HANG_MAX_S:
            problems.append("--hang-seconds must be between %g and %g" % (HANG_MIN_S, HANG_MAX_S))
        return problems

    def check_start_limit(self):
        p = self.lab.show(UNIT, ["ActiveState", "Result"])
        if p.get("ActiveState") == "failed" and p.get("Result") == "start-limit-hit":
            raise StartLimitHit(START_LIMIT_HELP)

    def state_problems(self) -> List[str]:
        """Everything that must be true before injecting a failure."""
        self.check_start_limit()
        problems = []
        p = self.lab.show(UNIT, ["LoadState", "ActiveState", "MainPID"])
        if p.get("LoadState") != "loaded":
            problems.append("%s is not loaded" % UNIT)
        if p.get("ActiveState") != "active":
            problems.append("%s is %s, not active" % (UNIT, p.get("ActiveState")))
        if self.lab.show(MONITOR_UNIT, ["ActiveState"]).get("ActiveState") != "active":
            problems.append("%s is not active: incidents would not be recorded" % MONITOR_UNIT)
        ok, hpid = self.lab.health()
        if not ok:
            problems.append("/health is not answering OK")
        elif str(hpid) != p.get("MainPID"):
            problems.append("/health PID %s differs from systemd MainPID %s"
                            % (hpid, p.get("MainPID")))
        st = self.lab.dashboard("/api/status")
        if st.get("error"):
            problems.append("dashboard: %s" % st["error"])
        elif st.get("stale"):
            problems.append("monitor data is stale (is demo-monitor running?)")
        else:
            status = st.get("status") or {}
            if status.get("open_incident"):
                problems.append("an incident is still open; wait for it to close")
            if status.get("status") != "healthy":
                problems.append("monitor status is %r, not 'healthy'" % status.get("status"))
        return problems

    def uptime(self) -> float:
        p = self.lab.show(UNIT, ["ActiveEnterTimestampMonotonic"])
        started = int(p.get("ActiveEnterTimestampMonotonic") or 0) / 1e6
        return self.lab.now() - started if started else 0.0

    def wait_ready(self):
        """Wait until the service has been up >= MIN_UPTIME_S and all checks pass."""
        deadline = self.lab.now() + READY_TIMEOUT_S
        announced = False
        while True:
            problems = self.state_problems()
            up = self.uptime()
            if not problems and up >= MIN_UPTIME_S:
                return
            if self.lab.now() > deadline:
                raise ExperimentError("not ready after %ds: %s" % (
                    READY_TIMEOUT_S, "; ".join(problems) or "uptime %.1f s" % up))
            if not announced and not problems:
                self.log("  waiting %.0f s: the service must be up %d s before the next "
                         "failure (spaces this script's own restarts)"
                         % (MIN_UPTIME_S - up, MIN_UPTIME_S))
                announced = True
            self.lab.sleep(1.0)

    def max_incident_id(self) -> int:
        items = self.lab.dashboard("/api/incidents").get("items") or []
        return max([i.get("id", 0) for i in items if isinstance(i.get("id"), int)] or [0])

    # -- hang handling -----------------------------------------------------
    def _stopped_in_unit(self) -> Tuple[Optional[List[int]], str]:
        """Which processes of the unit are stopped.

        Returns (list, observation). The list is:
          non-empty  these PIDs were seen stopped (a definite finding)
          []         EVERY process of the unit was read and none is stopped
          None       could not check: the cgroup or some process state was unreadable
        """
        pids = self.lab.unit_pids()
        if pids is None:
            return None, "the unit's process list could not be read"
        stopped, unreadable = [], []
        for p in pids:
            info = self.lab.process_info(p)
            if info is None:
                unreadable.append(p)
            elif info[0] in STOPPED_STATES:
                stopped.append(p)
        if stopped:
            return stopped, "stopped processes in unit: %s" % stopped
        if unreadable:
            return None, "state of unit process(es) %s could not be read" % unreadable
        return [], "checked %d unit process(es), none stopped" % len(pids)

    def check_resumed_once(self) -> Tuple[str, str]:
        """One look at the system after SIGCONT. Returns (status, observation):
          resumed      same process (PID + start time) runs (R/S/D), answers /health
                       with its PID, and every process of the unit was checked and
                       none is stopped
          stopped      the hung process (or another process of the unit) is stopped
          exited       the hung process is a zombie, dead or gone
          pid-changed  systemd's main PID is now a different process; nothing stopped
          unknown      not enough evidence (yet), including when the unit's
                       processes could not all be checked
        """
        pid, start = self.hang_pid or 0, self.hang_start
        try:
            main = int(self.lab.show(UNIT, ["MainPID"]).get("MainPID") or 0)
        except (ExperimentError, ValueError):
            main = -1
        info = self.lab.process_info(pid)
        same = info is not None and start is not None and info[1] == start
        state = info[0] if same else None
        stopped, unit_seen = self._stopped_in_unit()

        if same and state in STOPPED_STATES:
            return "stopped", "process %d is still stopped (state %s)" % (pid, state)
        if stopped:
            return "stopped", "process(es) %s of %s are stopped" % (stopped, UNIT)
        if same and state in EXITED_STATES:
            return "exited", "process %d has exited (state %s)" % (pid, state)
        if main > 0 and not (main == pid and same):
            # systemd's main process is no longer the process we stopped: either a
            # different PID, or the same PID number reused by a new process.
            change = ("main PID changed %d -> %d" % (pid, main) if main != pid else
                      "main process changed (PID %d reused by a new process)" % pid)
            if stopped is None:
                return "unknown", ("%s; whether any process of the unit is still stopped "
                                   "could not be checked (%s)" % (change, unit_seen))
            return "pid-changed", "%s during the hang; %s" % (change, unit_seen)
        if not same:
            what = "gone" if info is None else "PID %d now belongs to another process" % pid
            return "exited", "the hung process %d is %s" % (pid, what)
        if state in RUNNING_STATES:
            ok, hpid = self.lab.health()
            if ok and hpid == pid and main == pid:
                if stopped is None:
                    # The hung process itself runs, but other processes of the unit
                    # could not all be checked: do not claim more than we saw.
                    return "unknown", ("process %d runs (state %s) and /health answers "
                                       "with it, but whether any process of the unit is "
                                       "still stopped could not be checked: %s"
                                       % (pid, state, unit_seen))
                return "resumed", "process %d runs (state %s), /health answers with it, %s" % (
                    pid, state, unit_seen)
            return "unknown", "process %d state %s, but /health not answering with it yet" % (
                pid, state)
        return "unknown", "process %d in unexpected state %s" % (pid, state)

    def confirm_resumed(self) -> Tuple[str, str]:
        """Poll check_resumed_once until a final answer or RESUME_CONFIRM_S."""
        deadline = self.lab.now() + RESUME_CONFIRM_S
        while True:
            status, seen = self.check_resumed_once()
            if status in ("resumed", "pid-changed"):
                return status, seen
            if self.lab.now() >= deadline:
                return status, seen
            self.lab.sleep(0.25)

    def resume(self) -> bool:
        """Send SIGCONT and verify. Never raises. True only if no process of the
        unit is left stopped AND that was actually observed ("resumed" or
        "pid-changed"); self.last_resume_status says which."""
        if not self.stopped_service:
            return True
        self.resume_attempts += 1
        error = None
        try:
            self.lab.signal_main("SIGCONT", interactive=True)
        except ExperimentError as exc:
            error = str(exc)
        status, seen = self.confirm_resumed()
        self.last_resume_status, self.last_resume_seen = status, seen
        if status in ("resumed", "pid-changed"):
            self.stopped_service = False
            if status == "pid-changed":
                self.warn("Note: %s. No process of %s is stopped now, but this hang run did "
                          "not measure a hang of one process and is discarded. Something "
                          "else restarted the service, or the process exited."
                          % (seen, UNIT))
            if error:
                self.warn("Note: the SIGCONT command reported an error (%s), but %s."
                          % (error, seen))
            return True
        self.warn("WARNING: could not confirm that %s resumed after the hang "
                  "(SIGCONT attempt %d).\n"
                  "  SIGCONT command: %s\n  Observed: %s\n"
                  "  It may still be stopped. Run:\n  %s"
                  % (UNIT, self.resume_attempts, error or "sent without error", seen,
                     RESUME_HELP.replace("\n", "\n  ")))
        return False

    # -- one run -----------------------------------------------------------
    def inject(self):
        if self.scenario == "kill":
            self.lab.signal_main("SIGKILL")
        elif self.scenario == "crash":
            self.lab.crash()
        else:
            # Set the flag first: if we are interrupted right after SIGSTOP is
            # delivered, cleanup must still try SIGCONT (harmless if not stopped).
            self.stopped_service = True
            self.lab.signal_main("SIGSTOP")
            try:
                self.verify_stopped_target()
                self.lab.sleep(self.hang_s)
            finally:
                if not self.resume():
                    raise ResumeNotConfirmed("hang could not be confirmed as resumed")
            if self.last_resume_status == "pid-changed":
                raise PidChangedDuringHang(self.last_resume_seen)

    def verify_stopped_target(self):
        """Right after SIGSTOP: the process systemd stopped must be the one we
        recorded (same PID, same start time) and must actually be stopped."""
        main = int(self.lab.show(UNIT, ["MainPID"]).get("MainPID") or 0)
        info = self.lab.process_info(self.hang_pid or 0)
        if main != self.hang_pid or info is None or info[1] != self.hang_start:
            raise PidChangedDuringHang("main PID changed between reading it (%s) and "
                                       "SIGSTOP (now %s)" % (self.hang_pid, main))
        deadline = self.lab.now() + 2.0
        while info is not None and info[0] not in STOPPED_STATES:
            if self.lab.now() >= deadline:
                raise ExperimentError("process %d did not enter the stopped state after "
                                      "SIGSTOP (state %s)" % (self.hang_pid, info[0]))
            self.lab.sleep(0.05)
            info = self.lab.process_info(self.hang_pid)

    def wait_client_recovery(self, old_pid: int, since: float) -> Optional[float]:
        """Independent cross-check: first OK /health (right PID), polled every 0.1 s."""
        want_new = EXPECT[self.scenario]["pid_changes"]
        deadline = self.lab.now() + 30
        next_limit_check = self.lab.now()
        while self.lab.now() < deadline:
            ok, pid = self.lab.health()
            if ok and ((pid != old_pid) if want_new else (pid == old_pid)):
                return round(self.lab.now() - since, 3)
            if self.lab.now() >= next_limit_check:  # ~1/s, not every 0.1 s poll
                self.check_start_limit()
                next_limit_check = self.lab.now() + 1.0
            self.lab.sleep(0.1)
        self.check_start_limit()
        return None

    def wait_incident(self, base_id: int) -> List[dict]:
        timeout = INCIDENT_TIMEOUT_S + (self.hang_s if self.scenario == "hang" else 0)
        deadline = self.lab.now() + timeout

        def new_incidents() -> List[dict]:
            """Only incidents recorded after the run started (id > base_id)."""
            items = self.lab.dashboard("/api/incidents").get("items") or []
            return sorted([i for i in items if isinstance(i.get("id"), int)
                           and i["id"] > base_id], key=lambda i: i["id"])

        while self.lab.now() < deadline:
            if new_incidents():
                self.lab.sleep(2.0)  # let any second record land before judging
                return new_incidents()
            self.check_start_limit()
            self.lab.sleep(0.5)
        self.check_start_limit()
        raise ExperimentError("no incident was recorded within %d s" % timeout)

    def run_once(self, n: int) -> dict:
        self.wait_ready()
        p = self.lab.show(UNIT, ["MainPID", "NRestarts"])
        old_pid = int(p.get("MainPID") or 0)
        self.hang_pid = old_pid
        info = self.lab.process_info(old_pid)
        self.hang_start = info[1] if info else None
        if self.scenario == "hang" and self.hang_start is None:
            raise ExperimentError("cannot read /proc/%d/stat to identify the process; the "
                                  "hang scenario needs it to verify resumption" % old_pid)
        base_id = self.max_incident_id()
        self.log("  run %d/%d: %s, PID %d" % (n, self.runs, self.scenario, old_pid))
        injected_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        t0 = self.lab.now()
        try:
            self.inject()
        except PidChangedDuringHang as exc:
            self.save({"scenario": self.scenario, "run": n, "injected_at": injected_at,
                       "old_pid": old_pid, "outcome": "pid-changed-during-hang",
                       "detail": str(exc)})
            raise
        t_cmd = self.lab.now()
        since = t_cmd if self.scenario == "hang" else t0
        try:
            client = self.wait_client_recovery(old_pid, since)
            incidents = self.wait_incident(base_id)
        except StartLimitHit:
            self.save({"scenario": self.scenario, "run": n, "injected_at": injected_at,
                       "old_pid": old_pid, "outcome": "start-limit-hit"})
            raise
        inc = incidents[0]
        checks = check_expectations(self.scenario, inc, old_pid)
        checks["exactly_one_new_incident"] = len(incidents) == 1
        return {
            "scenario": self.scenario, "run": n, "outcome": "completed",
            "injected_at": injected_at,
            "hang_seconds": self.hang_s if self.scenario == "hang" else None,
            "incident_ids": [i.get("id") for i in incidents],
            "old_pid": old_pid, "new_pid": inc.get("new_pid"),
            "classification": inc.get("classification"), "kind": inc.get("kind"),
            "cause": inc.get("cause"), "timing_basis": inc.get("timing_basis"),
            "time_to_detect_s": inc.get("time_to_detect_s"),
            "restart_delay_s": inc.get("restart_delay_s"),
            "recovery_interval_s": [inc.get("recovery_time_lower_bound_s"),
                                    inc.get("recovery_time_s")],
            "undetected_window_before_estimate_s":
                inc.get("undetected_window_before_estimate_s"),
            "recovered_by": inc.get("recovered_by"),
            # Script-side measurement. Different starting point from the monitor's
            # interval (which starts at systemd's recorded exit): see CLIENT_START.
            "client_recovery_s": client,
            "client_recovery_start": CLIENT_START[self.scenario],
            "inject_command_s": round(t_cmd - t0, 3) if self.scenario != "hang" else None,
            "checks": checks, "all_checks_passed": all(checks.values()),
        }

    # -- output ------------------------------------------------------------
    def open_output(self):
        os.makedirs(self.out_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.out_path = os.path.join(self.out_dir, "%s-%s.jsonl" % (stamp, self.scenario))

    def save(self, rec: dict):
        if not self.out_path:
            return
        with open(self.out_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        # If run as root via sudo, give the file back to the invoking user.
        if os.geteuid() == 0 and os.environ.get("SUDO_UID"):
            os.chown(self.out_path, int(os.environ["SUDO_UID"]),
                     int(os.environ.get("SUDO_GID", os.environ["SUDO_UID"])))

    # -- main flow -----------------------------------------------------------
    def run(self, dry_run: bool = False) -> int:
        problems = self.validate_args()
        if problems:
            raise ExperimentError("; ".join(problems))
        problems = self.state_problems()
        if problems:
            raise ExperimentError("preconditions not met:\n  - " + "\n  - ".join(problems))
        self.log("Preconditions OK: %s active, monitor active, dashboard healthy." % UNIT)
        self.log("Plan: %d x %s%s. This script waits until the service has been up >= %d s "
                 "before each failure. Restarts from other sources also count toward "
                 "systemd's limit (StartLimitBurst=%d per %d s); if it is hit, the script "
                 "stops." % (self.runs, self.scenario,
                             " (%g s each)" % self.hang_s if self.scenario == "hang" else "",
                             MIN_UPTIME_S, START_LIMIT_BURST, START_LIMIT_INTERVAL_S))
        if dry_run:
            self.log("Dry run: nothing injected.")
            return 0
        if self.scenario != "crash":
            self.lab.validate_sudo()
        self.open_output()
        for n in range(1, self.runs + 1):
            rec = self.run_once(n)
            self.records.append(rec)
            self.save(rec)
            self.log("    -> incident #%s: %s, %s, interval %s s, client recovery %s s, "
                     "checks %s" % (",".join(map(str, rec["incident_ids"])), rec["kind"],
                                    rec["cause"] or "-", rec["recovery_interval_s"],
                                    rec["client_recovery_s"],
                                    "PASS" if rec["all_checks_passed"] else "FAIL %s" % [
                                        k for k, v in rec["checks"].items() if not v]))
            if not rec["all_checks_passed"]:
                self.log("Stopping: unexpected result (see above). No further failures "
                         "injected.")
                return 1
        return 0

    def summary(self):
        if not self.records:
            return
        self.log("\nResults (individual runs, not averages):")
        self.log("  %-4s %-8s %-14s %-12s %-18s %-10s" % (
            "run", "incident", "PID", "detect (s)", "interval (s)", "client (s)"))
        for r in self.records:
            lo, hi = r["recovery_interval_s"]
            self.log("  %-4d %-8s %-14s %-12s %-18s %-10s" % (
                r["run"], ",".join(map(str, r["incident_ids"])),
                "%s->%s" % (r["old_pid"], r["new_pid"]),
                "-" if r["time_to_detect_s"] is None else "%.3f" % r["time_to_detect_s"],
                "-" if hi is None else "%.3f-%.3f" % (lo, hi),
                "-" if r["client_recovery_s"] is None else "%.3f" % r["client_recovery_s"]))
        self.log("  interval = monitor, from systemd's recorded exit (hang: from first failed "
                 "check).\n  client = this script, %s; a different starting point, so the two "
                 "are not directly comparable." % CLIENT_START[self.scenario])
        if self.out_path:
            self.log("Saved to %s" % self.out_path)


def execute(exp: Experiment, dry_run: bool, lock_name: str = LOCK_NAME) -> int:
    """Lock, run, and always attempt hang cleanup. Returns the exit code."""
    try:
        lock = acquire_lock(lock_name)
    except ExperimentError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1
    rc = 1
    try:
        rc = exp.run(dry_run=dry_run)
    except Stop as why:
        print("\nStopped (%s). No further failures will be injected." % why)
    except StartLimitHit as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        rc = 3
    except ResumeNotConfirmed:
        rc = 4   # the warning with the recovery command was already printed
    except PidChangedDuringHang as exc:
        print("ERROR: %s. The run was discarded (saved with outcome "
              "\"pid-changed-during-hang\"); no further failures injected." % exc,
              file=sys.stderr)
    except ExperimentError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
    finally:
        for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                signal.signal(s, signal.SIG_IGN)
            except ValueError:
                pass  # not in the main thread (tests)
        if exp.stopped_service and not exp.resume():
            rc = 4
        exp.summary()
        lock.close()
    return rc


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Controlled failure experiments (demo-service only)")
    ap.add_argument("scenario", choices=sorted(EXPECT))
    ap.add_argument("--runs", type=int, default=3, help="1-%d (default 3)" % MAX_RUNS)
    ap.add_argument("--hang-seconds", type=float, default=5.0,
                    help="hang only: %g-%g s (default 5)" % (HANG_MIN_S, HANG_MAX_S))
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true", help="check preconditions only")
    args = ap.parse_args(argv)

    def on_signal(signum, _frame):
        raise Stop("signal %d" % signum)

    for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, on_signal)
    return execute(Experiment(Lab(), args.scenario, args.runs, args.hang_seconds,
                              args.out_dir), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
