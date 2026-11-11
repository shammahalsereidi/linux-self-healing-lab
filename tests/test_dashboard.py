"""Tests for dashboard/dashboard.py: binding, request handling, file reading,
escaping and the wording of measurements. No systemd needed.

Run:  python3 -m unittest discover -s tests -v
"""

import http.client
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "dashboard"))
import dashboard as db  # noqa: E402


def status_doc(status="healthy", **extra):
    doc = {
        "updated": "2026-11-11T16:00:00.000+00:00", "unit": "demo-service.service",
        "status": status,
        "current": {"pid": 4242, "nrestarts": 1, "cpu_pct": 0.4, "mem_bytes": 20 * 2 ** 20,
                    "mem_max": 128 * 2 ** 20, "health_ok": True, "health_ms": 1.2},
        "open_incident": None, "planned_events": [], "monitor_errors_total": 0,
        "history": [{"t": "2026-11-11T16:00:0%d.000+00:00" % i, "healthy": True,
                     "cpu_pct": 0.1 * i, "mem_bytes": (20 + i) * 2 ** 20, "health_ms": 1.0}
                    for i in range(5)],
        "recent_logs": ["2026-11-11T16:00:00.000+00:00 INFO started"],
    }
    doc.update(extra)
    return doc


CRASH = {"id": 1, "classification": "failure", "kind": "process-failure",
         "cause": "signal 9 (SIGKILL)", "old_pid": 10917, "new_pid": 11278,
         "failed_at": "2026-11-11T16:00:00.000+00:00", "time_to_detect_s": 0.632,
         "timing_basis": "systemd-exit-timestamp", "restart_delay_s": 2.001,
         "recovery_time_lower_bound_s": 2.107, "recovery_time_s": 2.636,
         "recovered_by": "systemd automatic restart (Restart=on-failure)",
         "evidence": "systemd state activating/auto-restart", "logs": []}


class DataDirTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write_status(self, doc, age=0.0):
        p = os.path.join(self.dir, "status.json")
        with open(p, "w") as f:
            json.dump(doc, f)
        t = time.time() - age
        os.utime(p, (t, t))

    def write_incidents(self, lines):
        with open(os.path.join(self.dir, "incidents.jsonl"), "w") as f:
            f.write("".join(lines))

    def page(self, stale_after=5.0):
        return db.render_page(db.read_status(self.dir), db.read_incidents(self.dir),
                              stale_after, self.dir)


# --------------------------------------------------------------------------
class BindTests(unittest.TestCase):
    def test_loopback_addresses_are_accepted(self):
        self.assertEqual(db.check_loopback("127.0.0.1"), db.socket.AF_INET)
        self.assertEqual(db.check_loopback("::1"), db.socket.AF_INET6)

    def test_non_loopback_addresses_are_refused(self):
        for host in ("0.0.0.0", "::", "192.168.1.10", "10.0.0.1"):
            with self.assertRaises(ValueError, msg=host):
                db.check_loopback(host)

    def test_hostnames_are_refused(self):
        # a name could resolve to anything; only IP literals are accepted
        with self.assertRaises(ValueError):
            db.check_loopback("localhost")

    def test_main_exits_instead_of_binding_publicly(self):
        with mock.patch("sys.stderr"):
            self.assertEqual(db.main(["--host", "0.0.0.0", "--port", "0"]), 2)


# --------------------------------------------------------------------------
class ReadingTests(DataDirTestCase):
    def test_missing_status_is_reported(self):
        res = db.read_status(self.dir)
        self.assertIsNone(res["data"])
        self.assertIn("not found", res["error"])

    def test_permission_error_explains_the_account(self):
        self.write_status(status_doc())
        with mock.patch("builtins.open", side_effect=PermissionError()):
            res = db.read_status(self.dir)
        self.assertIn("shlab-dash", res["error"])

    def test_partial_last_incident_line_is_skipped(self):
        self.write_incidents([json.dumps(CRASH) + "\n", '{"id": 2, "classif'])
        res = db.read_incidents(self.dir)
        self.assertEqual([i["id"] for i in res["items"]], [1])
        self.assertEqual(res["skipped"], 1)

    def test_incidents_newest_first_and_limited(self):
        self.write_incidents([json.dumps(dict(CRASH, id=i)) + "\n" for i in range(1, 8)])
        res = db.read_incidents(self.dir, limit=3)
        self.assertEqual([i["id"] for i in res["items"]], [7, 6, 5])
        self.assertTrue(res["truncated"])


# --------------------------------------------------------------------------
class RenderTests(DataDirTestCase):
    def test_healthy_page(self):
        self.write_status(status_doc())
        html = self.page()
        self.assertIn("Healthy", html)
        self.assertIn('class="chart"', html)
        self.assertIn("No incidents recorded yet", html)

    def test_stale_data_never_shows_healthy(self):
        self.write_status(status_doc("healthy"), age=60)
        html = self.page(stale_after=5)
        self.assertIn("Monitor data is stale", html)
        self.assertNotIn("badge-good", html)

    def test_untrusted_text_is_escaped(self):
        evil = '<script>alert(1)</script>'
        self.write_status(status_doc(recent_logs=[evil], unit=evil))
        self.write_incidents([json.dumps(dict(CRASH, cause=evil, evidence=evil,
                                              logs=[evil])) + "\n"])
        html = self.page()
        self.assertNotIn(evil, html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_no_inline_script_or_style(self):
        self.write_status(status_doc())
        html = self.page()
        self.assertNotIn("<script>", html)          # only <script src=...>
        self.assertNotIn(" style=", html)
        self.assertNotIn("<style", html)

    def test_crash_shows_interval_from_systemd_exit(self):
        self.write_status(status_doc())
        self.write_incidents([json.dumps(CRASH) + "\n"])
        html = self.page()
        self.assertIn("2.107–2.636 s", html)
        self.assertIn("from systemd&#x27;s recorded exit", html)
        self.assertIn("0.632 s", html)

    def test_hang_is_not_presented_as_a_bound_on_the_outage(self):
        hang = dict(CRASH, kind="unhealthy-but-running", cause=None,
                    timing_basis="first-unhealthy-poll", time_to_detect_s=None,
                    recovery_time_lower_bound_s=3.0, recovery_time_s=4.0,
                    undetected_window_before_estimate_s=0.95)
        self.write_status(status_doc())
        self.write_incidents([json.dumps(hang) + "\n"])
        html = self.page()
        self.assertIn("measured from the first failed check, not from the start", html)
        self.assertIn("up to 0.950 s earlier, or before that", html)
        self.assertNotIn("guarantee", html.lower())
        # the general note must not claim the interval always contains the outage
        self.assertNotIn("true value lies in the interval", html.lower())
        self.assertIn("may leave out the earlier part of the outage", html)

    def test_missed_window_says_not_measured(self):
        missed = dict(CRASH, timing_basis="unknown", recovery_time_s=None,
                      recovery_time_lower_bound_s=None, time_to_detect_s=None,
                      recovery_upper_bound_from_last_ok_s=1.05)
        self.write_status(status_doc())
        self.write_incidents([json.dumps(missed) + "\n"])
        self.assertIn("not measured", self.page())

    def test_uncertain_has_label_not_only_colour(self):
        unc = dict(CRASH, classification="uncertain", kind="restart-unclassified")
        self.write_status(status_doc())
        self.write_incidents([json.dumps(unc) + "\n"])
        html = self.page()
        self.assertIn("badge-warning", html)
        self.assertIn(">Uncertain</span>", html)

    def test_nice_ceil(self):
        self.assertEqual(db.nice_ceil(0.37), 0.5)
        self.assertEqual(db.nice_ceil(7), 10)
        self.assertEqual(db.nice_ceil(21), 25)
        self.assertEqual(db.nice_ceil(0), 1.0)


# --------------------------------------------------------------------------
class HttpTests(DataDirTestCase):
    def setUp(self):
        super().setUp()
        self.write_status(status_doc())
        self.srv = db.make_server("127.0.0.1", 0, self.dir, 5.0)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def request(self, method="GET", path="/", host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", host or "127.0.0.1:%d" % self.port)
        c.endheaders()
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_page_has_security_headers(self):
        r, body = self.request()
        self.assertEqual(r.status, 200)
        self.assertIn("script-src 'self'", r.getheader("Content-Security-Policy"))
        self.assertEqual(r.getheader("X-Content-Type-Options"), "nosniff")
        self.assertIn(b"Linux Self-Healing Lab", body)

    def test_localhost_name_is_accepted(self):
        r, _ = self.request(host="localhost:%d" % self.port)
        self.assertEqual(r.status, 200)

    def test_foreign_host_header_is_rejected(self):
        # DNS rebinding: attacker.example resolving to 127.0.0.1
        r, _ = self.request(host="attacker.example:%d" % self.port)
        self.assertEqual(r.status, 403)

    def test_write_methods_are_rejected(self):
        for method in ("POST", "PUT", "DELETE"):
            r, _ = self.request(method)
            self.assertEqual(r.status, 405, method)
            self.assertEqual(r.getheader("Allow"), "GET, HEAD")

    def test_api_status(self):
        r, body = self.request(path="/api/status")
        data = json.loads(body)
        self.assertEqual(data["status"]["status"], "healthy")
        self.assertFalse(data["stale"])

    def test_only_known_static_files_are_served(self):
        r, _ = self.request(path="/static/dashboard.js")
        self.assertEqual(r.status, 200)
        for path in ("/static/../dashboard.py", "/static/x", "/etc/passwd", "/status.json"):
            r, _ = self.request(path=path)
            self.assertEqual(r.status, 404, path)


if __name__ == "__main__":
    unittest.main()
