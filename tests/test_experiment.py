"""Tests for experiments/run_experiment.py with a simulated lab (fake clock,
fake systemd, fake monitor). Nothing real is signalled.

Run:  python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "experiments"))
import run_experiment as rx  # noqa: E402


class FakeLab:
    """Simulates demo-service under systemd + the monitor + the dashboard API.

    Processes are modelled like /proc: pid -> [state, start_time]. `self.pid` is
    systemd's MainPID, `self.unit` the PIDs in the unit's cgroup.
    """

    RESTART_SEC = 2.0

    def __init__(self, uptime=100.0):
        self.t = 10_000.0
        self.pid = 100
        self.procs = {100: ["S", 5000]}
        self.unit = {100}
        self.next_start = 6000
        self.next_pid = 101
        self.active_enter = self.t - uptime
        self.down_until = None        # process gone, restarting
        self.incidents = []
        self.next_id = 7              # pretend 6 earlier incidents exist
        self.calls = []               # privileged actions, in order
        self.starts = [self.active_enter]
        self.monitor_active = True
        self.stale = False
        self.open_incident = False
        self.health_pid_offset = 0
        self.cause_override = None
        self.interrupt_during_sleep = False
        self.start_limit_on_kill = None   # kill number that hits the start limit
        self.kills = 0
        self.failed = False               # unit in failed/start-limit-hit state
        self.sigcont_error = None         # ExperimentError message for SIGCONT
        self.sigcont_ineffective = False  # command "succeeds" but nothing resumes
        self.proc_readable = True
        self.cgroup_readable = True
        # edge cases for the hang scenario
        self.restart_before_stop = False  # service restarts between MainPID read and SIGSTOP
        self.restart_during_hang = False  # someone restarts the service while it is stopped
        self.old_stays_stopped = False    # ...and the old stopped process stays in the unit
        self.reuse_old_pid = False        # ...and its PID number is reused by another process
        self.new_main_same_pid = False    # ...and the NEW main gets the old PID number
        self.state_after_cont = None      # force an unusual state after SIGCONT
        self.zombie_on_cont = False       # process exits on SIGCONT (zombie, not yet reaped)

    @property
    def stopped(self):
        return self.procs.get(self.pid, ["?"])[0] == "T"

    # -- process helpers -----------------------------------------------------
    def _new_main(self):
        """systemd starts a new main process (the old one is no longer main)."""
        new = self.next_pid
        self.next_pid += 1
        self.procs[new] = ["S", self.next_start]
        self.next_start += 1000
        self.unit.add(new)
        self.pid = new
        self.active_enter = self.t
        self.starts.append(self.t)
        return new

    def _reap(self, pid):
        self.procs.pop(pid, None)
        self.unit.discard(pid)

    # -- clock -------------------------------------------------------------
    def now(self):
        return self.t

    def sleep(self, s):
        if self.interrupt_during_sleep and self.stopped:
            self.interrupt_during_sleep = False
            raise rx.Stop("simulated Ctrl+C")
        if self.restart_during_hang and self.stopped:
            self.restart_during_hang = False
            old = self.pid
            if not self.old_stays_stopped:
                self._reap(old)
            if self.new_main_same_pid:     # PID wraparound: same number, new process
                self.procs[old] = ["S", 777_777]
                self.unit.add(old)
                self.active_enter = self.t
                self.starts.append(self.t)
                self.t += s
                return
            self._new_main()
            if self.reuse_old_pid:
                self.procs[old] = ["S", 999_999]   # unrelated process, not in the unit
        self.t += s
        self._tick()

    def _tick(self):
        if self.down_until is not None and self.t >= self.down_until:
            old = self.pid
            self._reap(old)
            self.t, now = self.down_until, self.t
            new = self._new_main()
            self.t = now
            self._record(old, new, "process-failure", self._cause,
                         "systemd-exit-timestamp", "systemd automatic restart (Restart=on-failure)")
            self.down_until = None

    def _record(self, old, new, kind, cause, basis, recovered_by):
        self.incidents.append({
            "id": self.next_id, "classification": "failure", "kind": kind,
            "cause": self.cause_override or cause, "timing_basis": basis,
            "old_pid": old, "new_pid": new, "time_to_detect_s": 0.5,
            "restart_delay_s": 2.0 if new != old else None,
            "recovery_time_lower_bound_s": 2.1, "recovery_time_s": 2.6,
            "recovered_by": recovered_by})
        self.next_id += 1

    # -- systemd / proc / http ---------------------------------------------
    def show(self, unit, props):
        if unit == rx.MONITOR_UNIT:
            return {"ActiveState": "active" if self.monitor_active else "inactive"}
        assert unit == rx.UNIT
        down = self.down_until is not None
        if self.failed:
            return {"LoadState": "loaded", "ActiveState": "failed", "Result": "start-limit-hit",
                    "MainPID": "0", "NRestarts": "4", "ActiveEnterTimestampMonotonic": "0",
                    "ControlGroup": "/system.slice/demo-service.service"}
        return {"LoadState": "loaded", "ActiveState": "activating" if down else "active",
                "Result": "success",
                "MainPID": "0" if down else str(self.pid), "NRestarts": "0",
                "ActiveEnterTimestampMonotonic": str(int(self.active_enter * 1e6)),
                "ControlGroup": "/system.slice/demo-service.service"}

    def process_info(self, pid):
        if not self.proc_readable or pid not in self.procs:
            return None
        state, start = self.procs[pid]
        return state, start

    def unit_pids(self):
        return sorted(self.unit) if self.cgroup_readable else None

    def health(self):
        if self.down_until is not None or self.failed:
            return False, None
        if self.procs.get(self.pid, ["?"])[0] != "S":
            return False, None
        return True, self.pid + self.health_pid_offset

    def dashboard(self, path):
        if path == "/api/status":
            healthy = self.down_until is None and not self.stopped
            return {"error": None, "stale": self.stale, "status": {
                "status": "healthy" if healthy else "down",
                "open_incident": {"id": 1} if self.open_incident else None}}
        return {"items": list(reversed(self.incidents))}

    def validate_sudo(self):
        self.calls.append(("validate_sudo",))

    def signal_main(self, sig, interactive=False):
        self.calls.append(("signal", sig, self.t))
        if sig == "SIGKILL":
            self._cause = "signal 9 (SIGKILL)"
            self.kills += 1
            if self.kills == self.start_limit_on_kill:
                self.failed = True       # systemd refuses to start it again
                return
            self.down_until = self.t + self.RESTART_SEC
        elif sig == "SIGSTOP":            # --kill-whom=main: whatever is main NOW
            if self.restart_before_stop:
                self.restart_before_stop = False
                self._reap(self.pid)
                self._new_main()
            self.procs[self.pid][0] = "T"
        elif sig == "SIGCONT":            # --kill-whom=all: every process of the unit
            if self.sigcont_error:
                raise rx.ExperimentError(self.sigcont_error)
            if self.sigcont_ineffective:
                return
            resumed_main = self.stopped
            for p in self.unit:
                if self.procs.get(p, ["?"])[0] == "T":
                    self.procs[p][0] = "S"
            if self.state_after_cont:
                self.procs[self.pid][0] = self.state_after_cont
            elif self.zombie_on_cont:
                self.procs[self.pid][0] = "Z"
            elif resumed_main:
                self._record(self.pid, self.pid, "unhealthy-but-running", None,
                             "first-unhealthy-poll", "same process recovered (no restart)")

    def crash(self):
        self.calls.append(("crash", self.t))
        self._cause = "exit code 1"
        self.down_until = self.t + self.RESTART_SEC


class ExperimentTestCase(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        self.lines = []
        self.warnings = []

    def tearDown(self):
        shutil.rmtree(self.out)

    def make(self, lab, scenario="kill", runs=3, hang_s=5.0):
        return rx.Experiment(lab, scenario, runs, hang_s, self.out, log=self.lines.append,
                             warn=self.warnings.append)


class TargetingTests(unittest.TestCase):
    def test_kill_command_targets_only_the_demo_unit(self):
        for sig in ("SIGKILL", "SIGSTOP", "SIGCONT"):
            cmd = rx.kill_command(sig)
            self.assertEqual(cmd[-1], "demo-service.service")
            self.assertEqual([c for c in cmd if c.endswith(".service")], ["demo-service.service"])
        # failures hit the main process only; SIGCONT resumes every process of the unit
        self.assertIn("--kill-whom=main", rx.kill_command("SIGKILL"))
        self.assertIn("--kill-whom=main", rx.kill_command("SIGSTOP"))
        self.assertIn("--kill-whom=all", rx.kill_command("SIGCONT"))

    def test_other_signals_are_refused(self):
        with self.assertRaises(ValueError):
            rx.kill_command("SIGTERM")

    def test_old_systemd_uses_kill_who(self):
        self.assertEqual(rx.kill_whom_flag("systemd 249 (249.11-0ubuntu3)"), "--kill-who")
        self.assertEqual(rx.kill_whom_flag("systemd 255 (255.4-1ubuntu8)"), "--kill-whom")


class PreconditionTests(ExperimentTestCase):
    def assert_refused(self, lab, fragment):
        with self.assertRaises(rx.ExperimentError) as cm:
            self.make(lab).run()
        self.assertIn(fragment, str(cm.exception))
        self.assertEqual([c for c in lab.calls if c[0] in ("signal", "crash")], [])

    def test_monitor_not_running(self):
        lab = FakeLab()
        lab.monitor_active = False
        self.assert_refused(lab, "demo-monitor.service is not active")

    def test_stale_monitor_data(self):
        lab = FakeLab()
        lab.stale = True
        self.assert_refused(lab, "stale")

    def test_open_incident(self):
        lab = FakeLab()
        lab.open_incident = True
        self.assert_refused(lab, "still open")

    def test_health_pid_mismatch(self):
        lab = FakeLab()
        lab.health_pid_offset = 5
        self.assert_refused(lab, "differs from systemd MainPID")

    def test_run_limits(self):
        for runs in (0, rx.MAX_RUNS + 1):
            with self.assertRaises(rx.ExperimentError):
                self.make(FakeLab(), runs=runs).run()
        with self.assertRaises(rx.ExperimentError):
            self.make(FakeLab(), "hang", hang_s=60).run()

    def test_dry_run_injects_nothing(self):
        lab = FakeLab()
        self.assertEqual(self.make(lab).run(dry_run=True), 0)
        self.assertEqual(lab.calls, [])


class CrashLoopPacingTests(ExperimentTestCase):
    def test_waits_for_minimum_uptime_before_injecting(self):
        lab = FakeLab(uptime=3.0)  # service started 3 s ago
        self.make(lab, runs=1).run()
        t_kill = [c[2] for c in lab.calls if c[:2] == ("signal", "SIGKILL")][0]
        self.assertGreaterEqual(t_kill - (10_000.0 - 3.0), rx.MIN_UPTIME_S)

    def test_own_injections_never_exceed_four_starts_in_60s(self):
        # Holds for this script's own restarts only: starts from other sources
        # are not visible to it (see StartLimitTests).
        lab = FakeLab(uptime=0.0)
        self.assertEqual(self.make(lab, runs=rx.MAX_RUNS).run(), 0)
        starts = lab.starts
        self.assertEqual(len(starts), rx.MAX_RUNS + 1)
        for s in starts:
            in_window = [x for x in starts if s <= x <= s + rx.START_LIMIT_INTERVAL_S]
            self.assertLess(len(in_window), rx.START_LIMIT_BURST)


class ScenarioTests(ExperimentTestCase):
    def test_kill_runs_and_records(self):
        lab = FakeLab()
        exp = self.make(lab, "kill", runs=2)
        self.assertEqual(exp.run(), 0)
        self.assertEqual([r["incident_ids"] for r in exp.records], [[7], [8]])
        self.assertTrue(all(r["all_checks_passed"] for r in exp.records))
        with open(exp.out_path) as f:
            saved = [json.loads(l) for l in f]
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["cause"], "signal 9 (SIGKILL)")
        self.assertIsNotNone(saved[0]["client_recovery_s"])

    def test_crash_scenario(self):
        lab = FakeLab()
        exp = self.make(lab, "crash", runs=1)
        self.assertEqual(exp.run(), 0)
        self.assertEqual(exp.records[0]["cause"], "exit code 1")
        self.assertNotIn(("validate_sudo",), lab.calls)  # crash needs no sudo

    def test_hang_is_resumed_and_same_pid(self):
        lab = FakeLab()
        exp = self.make(lab, "hang", runs=1, hang_s=5)
        self.assertEqual(exp.run(), 0)
        sigs = [c[1] for c in lab.calls if c[0] == "signal"]
        self.assertEqual(sigs, ["SIGSTOP", "SIGCONT"])
        stop_t, cont_t = [c[2] for c in lab.calls if c[0] == "signal"]
        self.assertAlmostEqual(cont_t - stop_t, 5.0)
        self.assertEqual(exp.records[0]["old_pid"], exp.records[0]["new_pid"])
        self.assertIn("SIGCONT", exp.records[0]["client_recovery_start"])

    def test_hang_is_resumed_even_when_interrupted(self):
        lab = FakeLab()
        lab.interrupt_during_sleep = True
        exp = self.make(lab, "hang", runs=1)
        with self.assertRaises(rx.Stop):
            exp.run()
        self.assertFalse(lab.stopped)
        self.assertEqual([c[1] for c in lab.calls if c[0] == "signal"], ["SIGSTOP", "SIGCONT"])
        self.assertFalse(exp.stopped_service)

    def test_unexpected_result_stops_further_runs(self):
        lab = FakeLab()
        lab.cause_override = "signal 15 (SIGTERM)"
        exp = self.make(lab, "kill", runs=3)
        self.assertEqual(exp.run(), 1)
        self.assertEqual(len(exp.records), 1)
        self.assertFalse(exp.records[0]["checks"]["cause"])
        self.assertEqual(len([c for c in lab.calls if c[:2] == ("signal", "SIGKILL")]), 1)

    def test_existing_incidents_are_not_reported_as_results(self):
        lab = FakeLab()
        lab._record(1, 2, "process-failure", "x", "systemd-exit-timestamp", "y")  # old, id 7
        exp = self.make(lab, "kill", runs=1)
        exp.run()
        self.assertEqual(exp.records[0]["incident_ids"], [8])


    def test_old_incidents_never_count_even_with_slow_restart(self):
        lab = FakeLab()
        lab.RESTART_SEC = 8.0  # slower than the 2 s settle wait
        lab._record(1, 2, "process-failure", "x", "systemd-exit-timestamp", "y")
        exp = self.make(lab, "kill", runs=1)
        self.assertEqual(exp.run(), 0)
        self.assertEqual(exp.records[0]["incident_ids"], [8])
        self.assertEqual(exp.records[0]["old_pid"], 100)



# --------------------------------------------------------------------------
class LockTests(ExperimentTestCase):
    """Fix 1: one experiment per machine, whatever --out-dir is used."""

    NAME = "shlab-test-lock-%d" % os.getpid()

    def test_second_acquire_fails(self):
        held = rx.acquire_lock(self.NAME)
        try:
            with self.assertRaises(rx.ExperimentError):
                rx.acquire_lock(self.NAME)
        finally:
            held.close()
        rx.acquire_lock(self.NAME).close()  # released again

    def test_different_out_dirs_share_the_lock(self):
        other = tempfile.mkdtemp()
        held = rx.acquire_lock(self.NAME)
        try:
            for out in (self.out, other):
                lab = FakeLab()
                exp = rx.Experiment(lab, "kill", 1, 5, out, log=self.lines.append,
                                    warn=self.warnings.append)
                with contextlib.redirect_stderr(io.StringIO()):
                    rc = rx.execute(exp, False, lock_name=self.NAME)
                self.assertEqual(rc, 1)
                self.assertEqual(lab.calls, [])  # nothing injected
        finally:
            held.close()
            shutil.rmtree(other)

    def test_no_lock_file_is_created(self):
        rx.execute(self.make(FakeLab()), True, lock_name=self.NAME)
        self.assertEqual(os.listdir(self.out), [])


class StartLimitTests(ExperimentTestCase):
    """Fix 2: the start limit can be hit through other restarts; handle it."""

    def test_already_hit_before_start(self):
        lab = FakeLab()
        lab.failed = True
        with self.assertRaises(rx.StartLimitHit) as cm:
            self.make(lab).run()
        self.assertIn("reset-failed", str(cm.exception))
        self.assertEqual([c for c in lab.calls if c[0] == "signal"], [])

    def test_hit_during_run_stops_and_is_recorded(self):
        lab = FakeLab()
        lab.start_limit_on_kill = 2  # e.g. someone else restarted it meanwhile
        exp = self.make(lab, "kill", runs=3)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rx.execute(exp, False, lock_name="shlab-test-sl-%d" % os.getpid())
        self.assertEqual(rc, 3)
        self.assertIn("start-limit-hit", err.getvalue())
        self.assertIn("sudo systemctl reset-failed demo-service.service", err.getvalue())
        self.assertEqual(lab.kills, 2)  # no third injection
        with open(exp.out_path) as f:
            outcomes = [json.loads(l)["outcome"] for l in f]
        self.assertEqual(outcomes, ["completed", "start-limit-hit"])

    def test_documentation_does_not_promise_avoidance(self):
        self.assertIn("does NOT", rx.__doc__)
        self.assertIn("guarantee the limit is never reached", rx.__doc__)
        self.make(FakeLab()).run(dry_run=True)
        self.assertTrue(any("other sources also count" in l for l in self.lines))


class HangCleanupTests(ExperimentTestCase):
    """Fix 3: SIGCONT errors/timeouts are handled and resumption is verified."""

    def run_hang(self, lab):
        exp = self.make(lab, "hang", runs=1, hang_s=5)
        with contextlib.redirect_stderr(io.StringIO()):
            rc = rx.execute(exp, False, lock_name="shlab-test-hang-%d" % os.getpid())
        return exp, rc

    def test_sigcont_timeout_and_still_stopped_is_reported(self):
        lab = FakeLab()
        lab.sigcont_error = "sudo systemctl kill ... timed out after 30s"
        exp, rc = self.run_hang(lab)
        self.assertEqual(rc, 4)
        text = "\n".join(self.warnings)
        self.assertIn("could not confirm", text)
        self.assertIn("timed out", text)
        self.assertIn("still stopped (state T)", text)
        self.assertIn("sudo systemctl kill --kill-whom=all --signal=SIGCONT "
                      "demo-service.service", text)
        self.assertNotIn("guarantee", text.lower())
        self.assertTrue(exp.stopped_service)
        # cleanup retried once more and says which attempt failed
        self.assertEqual(exp.resume_attempts, 2)
        self.assertIn("(SIGCONT attempt 2)", text)

    def test_command_ok_but_process_still_stopped(self):
        lab = FakeLab()
        lab.sigcont_ineffective = True
        _, rc = self.run_hang(lab)
        self.assertEqual(rc, 4)
        self.assertIn("sent without error", "\n".join(self.warnings))

    def test_error_reported_but_process_running_is_confirmed(self):
        lab = FakeLab()
        lab.sigcont_error = "exit 1: some warning"
        exp = self.make(lab, "hang")
        exp.stopped_service, exp.hang_pid, exp.hang_start = True, lab.pid, 5000  # not stopped
        self.assertTrue(exp.resume())
        self.assertIn("but process 100 runs (state S)", "\n".join(self.warnings))

    def test_unreadable_proc_refuses_hang_before_injecting(self):
        # Without /proc the stopped process cannot be identified, so resumption
        # could not be verified: refuse instead of guessing.
        lab = FakeLab()
        lab.proc_readable = False
        with self.assertRaises(rx.ExperimentError) as cm:
            self.make(lab, "hang", runs=1).run()
        self.assertIn("/proc/100/stat", str(cm.exception))
        self.assertEqual([c for c in lab.calls if c[0] == "signal"], [])

    def test_unreadable_cgroup_is_unknown_even_if_process_is_healthy(self):
        # The hung process runs and answers /health, but the unit's other
        # processes could not be checked: that is not enough to say "resumed".
        lab = FakeLab()
        lab.cgroup_readable = False
        exp = self.make(lab, "hang", runs=1, hang_s=5)
        with contextlib.redirect_stderr(io.StringIO()):
            rc = rx.execute(exp, False, lock_name="shlab-test-cg-%d" % os.getpid())
        self.assertEqual(rc, 4)
        self.assertEqual(exp.last_resume_status, "unknown")
        text = "\n".join(self.warnings)
        self.assertIn("process list could not be read", text)
        self.assertIn("could not confirm", text)
        self.assertFalse(lab.stopped)   # it did resume; we just could not prove it fully

    def test_interrupt_with_failed_sigcont_still_reports(self):
        lab = FakeLab()
        lab.interrupt_during_sleep = True
        lab.sigcont_error = "timed out"
        _, rc = self.run_hang(lab)
        self.assertEqual(rc, 4)
        self.assertIn("could not confirm", "\n".join(self.warnings))

    def test_real_lab_turns_timeouts_into_errors(self):
        lab = rx.Lab.__new__(rx.Lab)
        lab.sudo, lab.flag = [], "--kill-whom"
        import subprocess as sp
        from unittest import mock
        for exc in (sp.TimeoutExpired("systemctl", 30), FileNotFoundError("systemctl")):
            with mock.patch.object(rx.subprocess, "run", side_effect=exc):
                with self.assertRaises(rx.ExperimentError):
                    lab.signal_main("SIGCONT", interactive=True)


class ClientRecoveryTests(ExperimentTestCase):
    """Fix 4: the script's own measurement is labelled with its starting point."""

    def test_kill_starts_before_the_systemctl_command(self):
        exp = self.make(FakeLab(), "kill", runs=1)
        exp.run()
        rec = exp.records[0]
        self.assertIn("before the sudo systemctl kill command", rec["client_recovery_start"])
        self.assertIn("includes its latency", rec["client_recovery_start"])
        self.assertIsNotNone(rec["inject_command_s"])

    def test_hang_starts_after_sigcont(self):
        exp = self.make(FakeLab(), "hang", runs=1)
        exp.run()
        self.assertIn("after the SIGCONT", exp.records[0]["client_recovery_start"])
        self.assertIsNone(exp.records[0]["inject_command_s"])

    def test_summary_says_not_directly_comparable(self):
        exp = self.make(FakeLab(), "kill", runs=1)
        exp.run()
        exp.summary()
        self.assertTrue(any("not directly comparable" in l for l in self.lines))



class ResumeIdentityTests(ExperimentTestCase):
    """Zombies, reused PIDs and PID changes are never reported as 'resumed'."""

    LOCK = "shlab-test-identity-%d" % os.getpid()

    def run_hang(self, lab):
        exp = self.make(lab, "hang", runs=1, hang_s=5)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = rx.execute(exp, False, lock_name=self.LOCK)
        return exp, rc, err.getvalue() + "\n".join(self.warnings)

    def test_parse_proc_stat_handles_spaces_and_parens_in_name(self):
        text = "1234 (python3 (x) y) S 1 1234 1234 0 -1 4194560 " + "0 " * 12 + "987654 rest"
        self.assertEqual(rx.parse_proc_stat(text), ("S", 987654))
        self.assertIsNone(rx.parse_proc_stat(None))
        self.assertIsNone(rx.parse_proc_stat("garbage"))

    def test_real_proc_stat_of_this_process(self):
        info = rx.Lab.__new__(rx.Lab).process_info(os.getpid())
        self.assertIsNotNone(info)
        self.assertIn(info[0], rx.RUNNING_STATES)
        self.assertGreater(info[1], 0)

    def test_zombie_is_not_resumed(self):
        lab = FakeLab()
        lab.zombie_on_cont = True
        exp, rc, text = self.run_hang(lab)
        self.assertEqual(rc, 4)
        self.assertEqual(exp.last_resume_status, "exited")
        self.assertIn("has exited (state Z)", text)
        self.assertNotIn("resumed\n", text)

    def test_restart_during_hang_is_reported_and_discarded(self):
        lab = FakeLab()
        lab.restart_during_hang = True
        exp, rc, text = self.run_hang(lab)
        self.assertEqual(rc, 1)
        self.assertEqual(exp.last_resume_status, "pid-changed")
        self.assertIn("main PID changed 100 -> 101 during the hang", text)
        self.assertIn("checked 1 unit process(es), none stopped", text)
        self.assertIn("discarded", text)
        self.assertFalse(exp.stopped_service)
        self.assertEqual(exp.records, [])  # not counted as a hang result
        with open(exp.out_path) as f:
            rec = json.loads(f.readline())
        self.assertEqual(rec["outcome"], "pid-changed-during-hang")
        self.assertIn("main PID changed 100 -> 101", rec["detail"])

    def test_reused_pid_is_not_mistaken_for_the_hung_process(self):
        lab = FakeLab()
        lab.restart_during_hang = True
        lab.reuse_old_pid = True   # PID 100 now belongs to an unrelated running process
        exp, rc, text = self.run_hang(lab)
        self.assertNotEqual(exp.last_resume_status, "resumed")
        self.assertEqual(exp.last_resume_status, "pid-changed")
        self.assertEqual(rc, 1)

    def test_old_process_left_stopped_in_unit_is_detected(self):
        lab = FakeLab()
        lab.restart_during_hang = True
        lab.old_stays_stopped = True
        lab.sigcont_ineffective = True
        exp, rc, text = self.run_hang(lab)
        self.assertEqual(rc, 4)
        self.assertEqual(exp.last_resume_status, "stopped")
        self.assertIn("still stopped (state T)", text)

    def test_sigcont_reaches_old_process_that_is_no_longer_main(self):
        # with --kill-whom=all the leftover stopped process is resumed too
        lab = FakeLab()
        lab.restart_during_hang = True
        lab.old_stays_stopped = True
        exp, rc, _ = self.run_hang(lab)
        self.assertEqual(lab.procs[100][0], "S")
        self.assertEqual(exp.last_resume_status, "pid-changed")
        self.assertEqual(rc, 1)

    def test_pid_change_with_unreadable_cgroup_is_not_called_safe(self):
        lab = FakeLab()
        lab.restart_during_hang = True
        lab.cgroup_readable = False
        exp, rc, text = self.run_hang(lab)
        self.assertEqual(exp.last_resume_status, "unknown")
        self.assertEqual(rc, 4)
        self.assertIn("could not be checked", text)

    def test_new_main_with_same_pid_number_is_a_change_not_a_resume(self):
        lab = FakeLab()
        lab.restart_during_hang = True
        lab.new_main_same_pid = True     # PID 100 again, but a different process
        exp, rc, text = self.run_hang(lab)
        self.assertEqual(exp.last_resume_status, "pid-changed")
        self.assertIn("PID 100 reused by a new process", text)
        self.assertEqual(rc, 1)

    def test_unusual_process_state_is_not_resumed(self):
        lab = FakeLab()
        lab.state_after_cont = "W"
        lab.health = lambda: (True, lab.pid)   # even if /health answered
        exp, rc, text = self.run_hang(lab)
        self.assertEqual(exp.last_resume_status, "unknown")
        self.assertIn("unexpected state W", text)
        self.assertEqual(rc, 4)

    def test_restart_between_pid_read_and_sigstop(self):
        lab = FakeLab()
        lab.restart_before_stop = True   # SIGSTOP hits the NEW main process
        exp, rc, text = self.run_hang(lab)
        self.assertEqual(rc, 1)
        self.assertIn("changed between reading it (100) and SIGSTOP (now 101)", text)
        self.assertEqual([c[1] for c in lab.calls if c[0] == "signal"], ["SIGSTOP", "SIGCONT"])
        self.assertFalse(lab.stopped)               # the new main was resumed
        self.assertFalse(exp.stopped_service)



class UnitCheckCompletenessTests(ExperimentTestCase):
    """'Nothing stopped' is only claimed when every unit process was read."""

    def exp_for(self, lab):
        exp = self.make(lab, "hang")
        exp.hang_pid, exp.hang_start = lab.pid, 5000
        return exp

    def test_all_readable_none_stopped(self):
        lab = FakeLab()
        stopped, seen = self.exp_for(lab)._stopped_in_unit()
        self.assertEqual(stopped, [])
        self.assertIn("checked 1 unit process(es), none stopped", seen)

    def test_unreadable_cgroup_returns_none(self):
        lab = FakeLab()
        lab.cgroup_readable = False
        self.assertIsNone(self.exp_for(lab)._stopped_in_unit()[0])

    def test_unreadable_process_state_is_not_treated_as_running(self):
        lab = FakeLab()
        lab.unit.add(555)          # in the cgroup, but its /proc entry cannot be read
        stopped, seen = self.exp_for(lab)._stopped_in_unit()
        self.assertIsNone(stopped)
        self.assertIn("[555] could not be read", seen)

    def test_stopped_process_is_reported_even_if_another_is_unreadable(self):
        lab = FakeLab()
        lab.unit.update({555, 556})
        lab.procs[556] = ["T", 4000]
        stopped, _ = self.exp_for(lab)._stopped_in_unit()
        self.assertEqual(stopped, [556])   # a definite finding wins

    def test_healthy_main_with_unreadable_unit_process_is_unknown(self):
        lab = FakeLab()
        lab.unit.add(555)
        status, seen = self.exp_for(lab).check_resumed_once()
        self.assertEqual(status, "unknown")
        self.assertIn("could not be checked", seen)

    def test_pid_change_with_unreadable_unit_process_is_unknown(self):
        lab = FakeLab()
        exp = self.exp_for(lab)
        lab._reap(100)
        lab._new_main()
        lab.unit.add(555)
        status, seen = exp.check_resumed_once()
        self.assertEqual(status, "unknown")
        self.assertIn("main PID changed 100 -> 101", seen)

    def test_hang_run_with_unreadable_unit_process_is_not_confirmed(self):
        lab = FakeLab()
        lab.unit.add(555)
        exp = self.make(lab, "hang", runs=1, hang_s=5)
        with contextlib.redirect_stderr(io.StringIO()):
            rc = rx.execute(exp, False, lock_name="shlab-test-up-%d" % os.getpid())
        self.assertEqual(rc, 4)
        self.assertEqual(exp.last_resume_status, "unknown")
        self.assertEqual(exp.records, [])

    def test_transient_unreadable_process_is_rechecked(self):
        # a process that exits between listing and reading gets another look
        lab = FakeLab()
        lab.unit.add(555)
        exp = self.exp_for(lab)
        real_sleep = lab.sleep

        def sleep(s):
            lab.unit.discard(555)      # it is gone from the cgroup on the next look
            real_sleep(s)
        lab.sleep = sleep
        status, _ = exp.confirm_resumed()
        self.assertEqual(status, "resumed")


if __name__ == "__main__":
    unittest.main()
