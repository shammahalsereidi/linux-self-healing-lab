"""Unit tests for the monitor: parsing, systemctl error handling, PID
cross-check, incident classification and timing bounds.

Run:  python3 -m unittest discover -s tests -v
Everything is driven by hand-made samples / fake commands: no systemd needed.
"""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "monitor"))
import monitor  # noqa: E402
from monitor import IncidentTracker, Sample  # noqa: E402


def sample(t, active="active", sub="running", pid=100, nrestarts=0, health_ok=True,
           result="success", start=None, exit_=None, code=None, status=None, cpu=None,
           systemd_ok=True, health_error=None):
    """A poll that started at t-0.05 and finished at t."""
    return Sample(mono=t, poll_start_mono=t - 0.05, wall=1_700_000_000 + t,
                  systemd_ok=systemd_ok,
                  systemd_error=None if systemd_ok else "systemctl timed out after 3.0s",
                  active=active, sub=sub, result=result, pid=pid, nrestarts=nrestarts,
                  mem_bytes=10_000_000, cpu_nsec=cpu, start_mono=start, exit_mono=exit_,
                  exit_code=code, exit_status=status, health_ok=health_ok,
                  health_pid=pid if health_ok else None,
                  health_error=health_error or (None if health_ok else "connection refused"),
                  health_start_mono=t - 0.03, health_end_mono=t - 0.01)


def crashed(t, exit_at, code=2, status=9, nrestarts=0, result="signal"):
    return sample(t, active="activating", sub="auto-restart", pid=0, nrestarts=nrestarts,
                  result=result, exit_=exit_at, code=code, status=status, health_ok=False)


SHOW_OK = "\n".join([
    "LoadState=loaded", "ActiveState=active", "SubState=running", "Result=success",
    "MainPID=4242", "NRestarts=0", "MemoryCurrent=1000", "MemoryMax=infinity",
    "CPUUsageNSec=5", "ExecMainStartTimestampMonotonic=1000000",
    "ExecMainExitTimestampMonotonic=0", "ExecMainCode=0", "ExecMainStatus=0"]) + "\n"


# --------------------------------------------------------------------------
class ParsingTests(unittest.TestCase):
    def test_parse_show(self):
        p = monitor.parse_show("MainPID=42\nResult=success\nEmpty=\n")
        self.assertEqual(p, {"MainPID": "42", "Result": "success", "Empty": ""})

    def test_to_int_handles_unset_values(self):
        self.assertEqual(monitor.to_int("123"), 123)
        self.assertIsNone(monitor.to_int("[not set]"))
        self.assertIsNone(monitor.to_int("infinity"))
        self.assertIsNone(monitor.to_int(str(2 ** 64 - 1)))
        self.assertIsNone(monitor.to_int(None))

    def test_monotonic_microseconds(self):
        self.assertEqual(monitor.mono_us_to_s("2500000"), 2.5)
        self.assertIsNone(monitor.mono_us_to_s("0"))

    def test_describe_exit(self):
        self.assertEqual(monitor.describe_exit(1, 1), "exit code 1")
        self.assertEqual(monitor.describe_exit(2, 9), "signal 9 (SIGKILL)")
        self.assertIsNone(monitor.describe_exit(0, 0))

    def test_cpu_percent(self):
        a = sample(10.0, cpu=1_000_000_000)
        b = sample(12.0, cpu=1_500_000_000)  # 0.5 s CPU over 2 s wall = 25 %
        self.assertEqual(monitor.cpu_percent(a, b), 25.0)
        # counter went backwards (cgroup recreated) -> no value instead of a negative %
        self.assertIsNone(monitor.cpu_percent(b, sample(13.0, cpu=0)))


# --------------------------------------------------------------------------
class SystemctlErrorTests(unittest.TestCase):
    """systemctl_show must turn every failure into an explicit error, never raise."""

    def run_with(self, **kw):
        with mock.patch.object(monitor.subprocess, "run", **kw):
            return monitor.systemctl_show("demo-service.service", 3.0)

    def test_success(self):
        props, err = self.run_with(return_value=subprocess.CompletedProcess([], 0, SHOW_OK, ""))
        self.assertIsNone(err)
        self.assertEqual(props["MainPID"], "4242")

    def test_timeout(self):
        props, err = self.run_with(side_effect=subprocess.TimeoutExpired("systemctl", 3.0))
        self.assertIsNone(props)
        self.assertIn("timed out", err)

    def test_not_installed(self):
        props, err = self.run_with(side_effect=FileNotFoundError())
        self.assertIsNone(props)
        self.assertEqual(err, "systemctl not found")

    def test_nonzero_exit(self):
        props, err = self.run_with(return_value=subprocess.CompletedProcess(
            [], 1, "", "Failed to connect to bus: No such file or directory\n"))
        self.assertIsNone(props)
        self.assertIn("exited 1: Failed to connect to bus", err)

    def test_missing_properties(self):
        props, err = self.run_with(return_value=subprocess.CompletedProcess(
            [], 0, "LoadState=loaded\nActiveState=active\n", ""))
        self.assertIsNone(props)
        self.assertIn("missing properties", err)

    def test_unit_not_found(self):
        out = SHOW_OK.replace("LoadState=loaded", "LoadState=not-found")
        props, err = self.run_with(return_value=subprocess.CompletedProcess([], 0, out, ""))
        self.assertIsNone(props)
        self.assertEqual(err, "unit LoadState=not-found")


# --------------------------------------------------------------------------
def props_with(pid, active="active"):
    p = monitor.parse_show(SHOW_OK)
    p["MainPID"], p["ActiveState"] = str(pid), active
    return p


class PidCrossCheckTests(unittest.TestCase):
    def collect(self, shows, health):
        shows = list(shows)
        calls = []

        def show(unit, timeout):
            calls.append(unit)
            return shows.pop(0)
        s = monitor.collect_sample("u", "url", 1, 1, show=show,
                                   health=lambda url, t: health + (0.0, 0.1))
        return s, len(calls)

    def test_pid_match_is_healthy(self):
        s, n = self.collect([(props_with(4242), None)], (True, 4242, None))
        self.assertTrue(s.healthy)
        self.assertEqual(n, 1)

    def test_mismatch_resolved_by_recheck(self):
        # process replaced between systemctl and /health: second query agrees
        s, n = self.collect([(props_with(4242), None), (props_with(5000), None)],
                            (True, 5000, None))
        self.assertTrue(s.healthy)
        self.assertEqual((s.pid, n), (5000, 2))

    def test_persistent_mismatch_is_unhealthy(self):
        # something else answers on the port
        s, _ = self.collect([(props_with(4242), None), (props_with(4242), None)],
                            (True, 7777, None))
        self.assertFalse(s.healthy)
        self.assertIn("PID mismatch", s.health_error)

    def test_health_without_pid_is_unhealthy(self):
        s, _ = self.collect([(props_with(4242), None), (props_with(4242), None)],
                            (True, None, None))
        self.assertFalse(s.healthy)

    def test_systemctl_error_marks_sample_untrusted(self):
        s, _ = self.collect([(None, "systemctl timed out after 1.0s")], (True, 4242, None))
        self.assertFalse(s.systemd_ok)
        self.assertFalse(s.healthy)
        self.assertIn("timed out", s.systemd_error)


# --------------------------------------------------------------------------
class TrackerFailureTests(unittest.TestCase):
    def test_sigkill_crash_timings_and_bounds(self):
        tr = IncidentTracker()
        self.assertEqual(tr.update(sample(10.0, pid=100, start=1.0)), [])
        # systemd recorded the exit at 10.4; poll 10.95-11.0 sees auto-restart
        ev = tr.update(crashed(11.0, exit_at=10.4))
        self.assertEqual([e["type"] for e in ev], ["open"])
        # poll 11.95-12.0 still down
        tr.update(sample(12.0, active="activating", sub="auto-restart", pid=0,
                         result="signal", exit_=10.4, code=2, status=9, health_ok=False))
        # new process started 12.4; poll 12.95-13.0 healthy
        inc = tr.update(sample(13.0, pid=200, nrestarts=1, start=12.4))[0]["incident"]
        self.assertEqual(inc["classification"], "failure")
        self.assertEqual(inc["cause"], "signal 9 (SIGKILL)")
        self.assertEqual((inc["old_pid"], inc["new_pid"]), (100, 200))
        self.assertEqual(inc["restarts_during_incident"], 1)
        self.assertEqual(inc["recovered_by"], "systemd automatic restart (Restart=on-failure)")
        self.assertAlmostEqual(inc["time_to_detect_s"], 0.6)
        self.assertEqual(inc["timing_basis"], "systemd-exit-timestamp")
        self.assertIsNone(inc["undetected_window_before_estimate_s"])
        self.assertAlmostEqual(inc["restart_delay_s"], 2.0)
        # true recovery lies between max(last unhealthy poll start, new process
        # start) and the end of the healthy poll
        self.assertAlmostEqual(inc["recovery_time_lower_bound_s"], 12.4 - 10.4)
        self.assertAlmostEqual(inc["recovery_time_s"], 13.0 - 10.4)
        self.assertAlmostEqual(inc["max_poll_gap_s"], 1.0)
        self.assertEqual(tr.status, "healthy")

    def test_lower_bound_uses_last_unhealthy_poll_when_later(self):
        tr = IncidentTracker()
        tr.update(sample(1.0))
        tr.update(crashed(2.0, exit_at=1.5))
        # process started at 2.2 but was not answering yet at poll 2.95-3.0
        tr.update(sample(3.0, pid=101, nrestarts=1, start=2.2, health_ok=False))
        inc = tr.update(sample(4.0, pid=101, nrestarts=1, start=2.2))[0]["incident"]
        self.assertAlmostEqual(inc["recovery_time_lower_bound_s"], 2.95 - 1.5)
        self.assertAlmostEqual(inc["recovery_time_s"], 4.0 - 1.5)

    def test_exit_code_crash(self):
        tr = IncidentTracker()
        tr.update(sample(1.0))
        tr.update(crashed(2.0, exit_at=1.5, code=1, status=1, result="exit-code"))
        inc = tr.update(sample(4.0, pid=101, nrestarts=1, start=3.5))[0]["incident"]
        self.assertEqual(inc["cause"], "exit code 1")
        self.assertAlmostEqual(inc["recovery_time_s"], 2.5)

    def test_failure_without_exit_timestamp_gives_no_exact_time(self):
        tr = IncidentTracker()
        tr.update(sample(1.0))
        tr.update(sample(2.0, active="failed", sub="failed", pid=0, result="exit-code",
                         health_ok=False))
        inc = tr.update(sample(5.0, pid=101, start=4.5))[0]["incident"]
        self.assertIsNone(inc["recovery_time_s"])
        self.assertIsNone(inc["failed_at_source"])
        self.assertEqual(inc["timing_basis"], "unknown")
        self.assertAlmostEqual(inc["recovery_upper_bound_from_last_ok_s"], 5.0 - 0.95)

    def test_hang_needs_threshold_and_uses_estimate(self):
        tr = IncidentTracker(fail_threshold=2)
        tr.update(sample(1.0))
        self.assertEqual(tr.update(sample(2.0, health_ok=False)), [])
        self.assertEqual(tr.status, "degraded")
        ev = tr.update(sample(3.0, health_ok=False))
        self.assertEqual(ev[0]["incident"]["kind"], "unhealthy-but-running")
        inc = tr.update(sample(6.0))[0]["incident"]  # same PID recovered by itself
        self.assertFalse(inc["process_replaced"])
        self.assertTrue(inc["failed_at_source"].startswith("estimate"))
        # measured from detection, not from the (unknown) start of the hang
        self.assertEqual(inc["timing_basis"], "first-unhealthy-poll")
        self.assertAlmostEqual(inc["recovery_time_s"], 6.0 - 1.95)
        self.assertIsNone(inc["time_to_detect_s"])  # meaningless without a start time
        # the hang may have begun anywhere after the last healthy poll ended (1.0)
        self.assertAlmostEqual(inc["undetected_window_before_estimate_s"], 1.95 - 1.0)

    def test_single_blip_does_not_open_incident(self):
        tr = IncidentTracker(fail_threshold=2)
        tr.update(sample(1.0))
        tr.update(sample(2.0, health_ok=False))
        self.assertEqual(tr.update(sample(3.0)), [])
        self.assertEqual(tr.status, "healthy")

    def test_crash_loop_then_manual_recovery(self):
        tr = IncidentTracker()
        tr.update(sample(1.0, nrestarts=3))
        tr.update(crashed(2.0, exit_at=1.5, code=1, status=1, nrestarts=3, result="exit-code"))
        tr.update(sample(9.0, active="failed", sub="failed", pid=0, nrestarts=5,
                         result="start-limit-hit", exit_=8.0, code=1, status=1,
                         health_ok=False))
        self.assertEqual(tr.status, "failed")
        # admin: reset-failed + start -> counter reset to 0
        inc = tr.update(sample(30.0, pid=500, nrestarts=0, start=29.5))[0]["incident"]
        self.assertTrue(inc["gave_up"])
        self.assertAlmostEqual(inc["failed_at_mono"], 1.5)  # first failure is kept
        self.assertIsNone(inc["restarts_during_incident"])  # counter was reset
        self.assertIn("likely manual", inc["recovered_by"])

    def test_stuck_transition_becomes_incident_after_grace(self):
        tr = IncidentTracker(transition_grace=10)
        tr.update(sample(1.0))
        for t in (2.0, 5.0, 11.0):
            ev = tr.update(sample(t, active="deactivating", sub="stop-sigterm", health_ok=False))
            self.assertEqual(ev, [])
        ev = tr.update(sample(12.0, active="deactivating", sub="stop-sigterm", health_ok=False))
        self.assertEqual(ev[0]["incident"]["kind"], "stuck-transition")


# --------------------------------------------------------------------------
class TrackerRestartClassificationTests(unittest.TestCase):
    """Issue 3: a PID change is not automatically a failure."""

    def test_clean_stop_and_start_is_planned_not_incident(self):
        tr = IncidentTracker()
        tr.update(sample(1.0, pid=100))
        self.assertEqual(tr.update(sample(2.0, active="inactive", sub="dead", pid=0,
                                          health_ok=False)), [])
        self.assertEqual(tr.status, "stopped")
        ev = tr.update(sample(5.0, pid=300))
        self.assertEqual([e["type"] for e in ev], ["planned"])
        self.assertEqual(len(tr.planned_events), 1)

    def test_manual_restart_seen_mid_transition_is_uncertain(self):
        tr = IncidentTracker()
        tr.update(sample(1.0, pid=100))
        tr.update(sample(2.0, active="deactivating", sub="stop-sigterm", health_ok=False))
        ev = tr.update(sample(3.0, pid=101, start=2.5))
        inc = ev[-1]["incident"]
        self.assertEqual(inc["classification"], "uncertain")
        self.assertEqual(inc["kind"], "restart-unclassified")
        self.assertIn("deactivating/stop-sigterm", inc["evidence"])
        self.assertIsNone(inc["recovery_time_s"])

    def test_pid_change_without_counter_is_uncertain(self):
        tr = IncidentTracker()
        tr.update(sample(1.0, pid=100))
        inc = tr.update(sample(2.0, pid=101, start=1.8))[-1]["incident"]
        self.assertEqual(inc["classification"], "uncertain")
        self.assertTrue(inc["cause"].startswith("uncertain"))

    def test_pid_change_with_counter_is_failure(self):
        tr = IncidentTracker()
        tr.update(sample(1.0, pid=100))
        ev = tr.update(sample(2.0, pid=101, nrestarts=1, start=1.8))
        self.assertEqual([e["type"] for e in ev], ["open", "close"])
        inc = ev[1]["incident"]
        self.assertEqual(inc["classification"], "failure")
        self.assertTrue(inc["missed_down_window"])
        self.assertIn("NRestarts 0 -> 1", inc["evidence"])
        self.assertIsNone(inc["recovery_time_s"])           # exact time unknown
        self.assertAlmostEqual(inc["recovery_upper_bound_from_last_ok_s"], 2.0 - 0.95)


# --------------------------------------------------------------------------
class TrackerDataQualityTests(unittest.TestCase):
    """Issues 4 and 5: mismatched PIDs and systemctl errors."""

    def test_systemctl_error_never_opens_incident(self):
        tr = IncidentTracker(fail_threshold=2)
        tr.update(sample(1.0))
        for t in (2.0, 3.0, 4.0):
            self.assertEqual(tr.update(sample(t, systemd_ok=False, health_ok=False)), [])
        self.assertEqual(tr.status, "monitor-error")
        self.assertEqual(tr.monitor_errors, 3)
        self.assertEqual(tr.update(sample(5.0)), [])  # same PID: nothing happened
        self.assertEqual(tr.status, "healthy")

    def test_systemctl_error_never_closes_incident(self):
        tr = IncidentTracker()
        tr.update(sample(1.0))
        tr.update(crashed(2.0, exit_at=1.5))
        # health answers but systemd data is unavailable: incident stays open
        self.assertEqual(tr.update(sample(3.0, pid=200, systemd_ok=False)), [])
        self.assertIsNotNone(tr.incident)
        inc = tr.update(sample(4.0, pid=200, nrestarts=1, start=3.5))[0]["incident"]
        self.assertEqual(inc["monitor_errors"], 1)

    def test_other_process_on_port_does_not_close_incident(self):
        tr = IncidentTracker()
        tr.update(sample(1.0))
        tr.update(crashed(2.0, exit_at=1.5))
        # systemd says active pid 200, but /health answered with another PID
        mismatch = sample(3.0, pid=200, nrestarts=1, health_ok=False,
                          health_error="PID mismatch: /health reports pid=999 but "
                                       "systemd MainPID=200")
        self.assertEqual(tr.update(mismatch), [])
        self.assertIsNotNone(tr.incident)

    def test_persistent_mismatch_while_running_is_incident(self):
        tr = IncidentTracker(fail_threshold=2)
        tr.update(sample(1.0))
        err = "PID mismatch: /health reports pid=999 but systemd MainPID=100"
        tr.update(sample(2.0, health_ok=False, health_error=err))
        ev = tr.update(sample(3.0, health_ok=False, health_error=err))
        self.assertEqual(ev[0]["incident"]["kind"], "unhealthy-but-running")
        self.assertIn("PID mismatch", ev[0]["incident"]["evidence"])


# --------------------------------------------------------------------------
class FileTests(unittest.TestCase):
    def test_output_files_are_owner_rw_group_r(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.umask(0o022)  # even with a permissive umask
            try:
                monitor.write_data(os.path.join(d, "status.json"), "{}")
                monitor.append_data(os.path.join(d, "incidents.jsonl"), "{}")
            finally:
                os.umask(old)
            for name in ("status.json", "incidents.jsonl"):
                mode = stat.S_IMODE(os.stat(os.path.join(d, name)).st_mode)
                self.assertEqual(mode, 0o640, name)

    def test_leftover_temp_file_is_replaced_not_reused(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "status.json")
            with open(p + ".tmp", "w") as f:
                f.write("old")
            os.chmod(p + ".tmp", 0o600)
            monitor.write_data(p, "{}")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o640)
            self.assertFalse(os.path.exists(p + ".tmp"))

    def test_read_app_log_window(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "demo.log")
            with open(p, "w") as f:
                f.write("2026-01-01T10:00:00.000+00:00 INFO old\n"
                        "2026-01-01T10:00:05.000+00:00 ERROR CRASH\n"
                        "not a timestamped line\n"
                        "2026-01-01T10:00:09.000+00:00 INFO started\n")
            t0 = monitor.datetime.fromisoformat("2026-01-01T10:00:04+00:00").timestamp()
            got = monitor.read_app_log(p, since=t0, until=t0 + 10)
            self.assertEqual([l.split()[-1] for l in got], ["CRASH", "started"])
            self.assertEqual(len(monitor.read_app_log(p)), 4)
            self.assertEqual(monitor.read_app_log(os.path.join(d, "missing.log")), [])


if __name__ == "__main__":
    unittest.main()
