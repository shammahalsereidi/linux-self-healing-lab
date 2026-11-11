"""Unit tests for installer/lab_install.py.

A FakeHost simulates accounts, groups and systemctl; files go to a temporary
directory used as the filesystem root. Nothing on the real system is touched.

Run:  python3 -m unittest discover -s tests -v
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "installer"))
import lab_install as li  # noqa: E402


NOT_FOUND = {"LoadState": "not-found", "FragmentPath": "", "DropInPaths": "",
             "ActiveState": "inactive", "MainPID": "0"}


class FakeHost(li.Host):
    def __init__(self, root):
        super().__init__(root=root)
        self.accounts = {}      # name -> {"uid", "gid", "gecos"}
        self.groups = {}        # name -> {"gid", "members"}
        self.unit_info = {}     # unit -> dict of systemctl show properties
        self.query_error = None
        self.commands = []
        self._next_id = 990
        self.stop_fails = {}     # unit -> stderr: `systemctl disable --now` fails
        self.stop_hangs = set()  # units that report success but keep running
        self.interrupt_after_useradd = None  # account name: simulate a crash
        self.fixed_files = []

    def account(self, name):
        return dict(self.accounts[name]) if name in self.accounts else None

    def group(self, name):
        return dict(self.groups[name]) if name in self.groups else None

    def query(self, cmd):
        if self.query_error:
            raise self.query_error
        info = dict(NOT_FOUND)
        info.update(self.unit_info.get(cmd[2], {}))
        out = "".join("%s=%s\n" % kv for kv in info.items())
        return subprocess.CompletedProcess(cmd, 0, out, "")

    def say(self, msg):
        pass

    def chown(self, real_path, owner, group):
        pass

    def fix_file(self, path, owner, group, mode):
        self.fixed_files.append((path, owner, group, mode))
        os.chmod(self.p(path), mode)

    def run(self, cmd, check=True):
        self.commands.append(cmd)
        if cmd[0] == "useradd":
            name, gecos = cmd[-1], cmd[cmd.index("--comment") + 1]
            self._next_id += 1
            self.accounts[name] = {"uid": self._next_id, "gid": self._next_id, "gecos": gecos}
            self.groups[name] = {"gid": self._next_id, "members": []}
            if self.interrupt_after_useradd == name:
                raise KeyboardInterrupt("simulated crash right after useradd")
        elif cmd[0] == "userdel":
            self.accounts.pop(cmd[1], None)
            self.groups.pop(cmd[1], None)
        elif cmd[:2] == ["systemctl", "restart"]:
            # systemd now runs the unit from our file
            self.unit_info[cmd[2]] = {"LoadState": "loaded", "ActiveState": "active",
                                      "MainPID": "1234",
                                      "FragmentPath": os.path.join(li.UNIT_DIR, cmd[2])}
        elif cmd[:3] == ["systemctl", "disable", "--now"]:
            unit = cmd[3]
            if unit in self.stop_fails:
                return subprocess.CompletedProcess(cmd, 1, "", self.stop_fails[unit])
            if unit not in self.stop_hangs and unit in self.unit_info:
                self.unit_info[unit].update(ActiveState="inactive", MainPID="0")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    # helpers for tests
    def exists(self, path):
        return os.path.lexists(self.p(path))

    def put(self, path, text=""):
        os.makedirs(os.path.dirname(self.p(path)), exist_ok=True)
        with open(self.p(path), "w") as f:
            f.write(text)

    def changing_commands(self):
        return [c for c in self.commands if c[0] in ("useradd", "userdel", "groupdel")
                or (c[0] == "systemctl" and c[1] != "show")]


class InstallerTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.host = FakeHost(self.tmp)
        os.makedirs(self.host.p(li.UNIT_DIR))  # always present on a systemd system

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def snapshot(self):
        """All paths under the fake root, to prove nothing changed."""
        out = []
        for d, dirs, files in os.walk(self.tmp):
            out += [os.path.join(d, x) for x in dirs + files]
        return sorted(out)

    def assert_refused(self, *expected_fragments):
        before = self.snapshot()
        with self.assertRaises(li.InstallError) as cm:
            li.install(self.host, REPO)
        msg = str(cm.exception)
        self.assertIn("nothing was changed", msg)
        for frag in expected_fragments:
            self.assertIn(frag, msg)
        self.assertEqual(self.snapshot(), before, "filesystem changed despite refusal")
        self.assertEqual(self.host.changing_commands(), [])
        return msg


class FreshInstallTests(InstallerTestCase):
    def test_fresh_install_creates_and_records_everything(self):
        li.install(self.host, REPO)
        m = json.load(open(self.host.p(li.MANIFEST)))
        self.assertEqual(set(m["accounts"]), {"shlab-demo", "shlab-mon", "shlab-dash"})
        self.assertEqual(set(m["dirs"]), set(li.MANAGED_DIRS))
        self.assertEqual(set(m["units"]), set(li.UNITS))
        for unit in li.UNITS:
            self.assertTrue(li.unit_has_marker(self.host, unit))
        self.assertTrue(self.host.exists("/opt/self-healing-lab/monitor/monitor.py"))
        self.assertEqual(os.stat(self.host.p(li.STATE_DIR)).st_mode & 0o7777, 0o2750)
        self.assertTrue(self.host.exists("/opt/self-healing-lab/dashboard/static/dashboard.js"))
        self.assertEqual(os.stat(self.host.p("/opt/self-healing-lab/dashboard")).st_mode
                         & 0o777, 0o755)
        self.assertEqual(os.stat(self.host.p(li.LOG_DIR)).st_mode & 0o7777, 0o2750)

    def test_reinstall_reuses_only_own_resources(self):
        li.install(self.host, REPO)
        uids = {n: a["uid"] for n, a in self.host.accounts.items()}
        self.host.commands.clear()
        li.install(self.host, REPO)  # second run = update
        self.assertFalse([c for c in self.host.commands if c[0] == "useradd"])
        self.assertEqual({n: a["uid"] for n, a in self.host.accounts.items()}, uids)

    def test_dry_run_changes_nothing(self):
        self.host.dry_run = True
        before = self.snapshot()
        li.install(self.host, REPO)
        self.assertEqual(self.snapshot(), before)


class CollisionTests(InstallerTestCase):
    """Issue 1: never reuse or later delete something we did not create."""

    def test_existing_unrelated_account(self):
        self.host.accounts["shlab-mon"] = {"uid": 1001, "gid": 1001, "gecos": "Someone Else"}
        self.assert_refused("account 'shlab-mon' already exists")

    def test_existing_account_even_with_same_comment(self):
        # comment matches, but no manifest says we created it
        self.host.accounts["shlab-demo"] = {"uid": 1002, "gid": 1002,
                                            "gecos": li.ACCOUNTS["shlab-demo"]}
        self.assert_refused("account 'shlab-demo' already exists")

    def test_existing_group_without_account(self):
        self.host.groups["shlab-mon"] = {"gid": 1003, "members": ["alice"]}
        self.assert_refused("group 'shlab-mon' already exists")

    def test_existing_state_directory(self):
        os.makedirs(self.host.p(li.STATE_DIR))
        self.assert_refused("/var/lib/self-healing-lab already exists")

    def test_symlinked_log_directory(self):
        os.makedirs(self.host.p("/var/log"))
        os.symlink("/etc", self.host.p(li.LOG_DIR))
        self.assert_refused("/var/log/self-healing-lab is a symlink")

    def test_existing_unmanaged_unit_file(self):
        self.host.put("/etc/systemd/system/demo-monitor.service", "[Service]\nExecStart=/x\n")
        self.assert_refused("demo-monitor.service already exists and is not managed")

    def test_unit_defined_by_a_package(self):
        self.host.unit_info["demo-monitor.service"] = {
            "LoadState": "loaded", "DropInPaths": "",
            "FragmentPath": "/usr/lib/systemd/system/demo-monitor.service"}
        self.assert_refused("already defined by /usr/lib/systemd/system/demo-monitor.service")

    def test_masked_unit(self):
        self.host.unit_info["demo-service.service"] = {
            "LoadState": "masked", "FragmentPath": "", "DropInPaths": ""}
        self.assert_refused("demo-service.service is masked")

    def test_drop_in_overrides(self):
        self.host.unit_info["demo-service.service"] = {
            "LoadState": "not-found", "FragmentPath": "",
            "DropInPaths": "/etc/systemd/system/demo-service.service.d/override.conf"}
        self.assert_refused("has drop-in overrides")

    def test_missing_unit_directory(self):
        os.rmdir(self.host.p(li.UNIT_DIR))
        self.assert_refused("/etc/systemd/system does not exist")

    def test_systemctl_unavailable(self):
        self.host.query_error = FileNotFoundError()
        self.assert_refused("systemctl not found")

    def test_systemctl_timeout(self):
        self.host.query_error = subprocess.TimeoutExpired("systemctl", 15)
        self.assert_refused("systemctl timed out")

    def test_step1_legacy_install_is_detected(self):
        # Step 1 left /opt/self-healing-lab and an unmarked unit, and no manifest
        os.makedirs(self.host.p(li.OPT_DIR))
        self.host.put("/etc/systemd/system/demo-service.service", "[Unit]\n")
        msg = self.assert_refused("/opt/self-healing-lab already exists")
        self.assertIn("Upgrading from Step 1", msg)

    def test_all_problems_reported_at_once(self):
        self.host.accounts["shlab-mon"] = {"uid": 1, "gid": 1, "gecos": "x"}
        os.makedirs(self.host.p(li.STATE_DIR))
        self.assert_refused("account 'shlab-mon'", "/var/lib/self-healing-lab")


class UninstallTests(InstallerTestCase):
    def test_uninstall_without_manifest_touches_nothing(self):
        self.host.accounts["shlab-mon"] = {"uid": 1001, "gid": 1001, "gecos": "Someone"}
        os.makedirs(self.host.p(li.STATE_DIR))
        with self.assertRaises(li.InstallError):
            li.uninstall(self.host)
        self.assertIn("shlab-mon", self.host.accounts)
        self.assertTrue(self.host.exists(li.STATE_DIR))
        self.assertEqual(self.host.changing_commands(), [])

    def test_full_roundtrip(self):
        li.install(self.host, REPO)
        self.assertEqual(li.uninstall(self.host), 0)
        self.assertEqual(self.host.accounts, {})
        for path in li.MANAGED_DIRS:
            self.assertFalse(self.host.exists(path), path)
        for unit in li.UNITS:
            self.assertFalse(self.host.exists(os.path.join(li.UNIT_DIR, unit)))

    def test_account_replaced_after_install_is_kept(self):
        li.install(self.host, REPO)
        # someone deleted our account and created an unrelated one with the same name
        self.host.accounts["shlab-mon"] = {"uid": 4000, "gid": 4000, "gecos": "Unrelated"}
        self.assertEqual(li.uninstall(self.host), 1)
        self.assertIn("shlab-mon", self.host.accounts)
        self.assertNotIn(["userdel", "shlab-mon"], self.host.commands)
        self.assertNotIn("shlab-demo", self.host.accounts)  # ours: removed
        self.assertTrue(self.host.exists(li.MANIFEST))      # kept for the record

    def test_unit_file_replaced_after_install_is_kept(self):
        li.install(self.host, REPO)
        self.host.put("/etc/systemd/system/demo-monitor.service", "[Service]\n")
        # the replaced (foreign) unit is running: we may not stop it, so nothing
        # that it might use is deleted either
        self.assertEqual(li.uninstall(self.host), 2)
        self.assertTrue(self.host.exists("/etc/systemd/system/demo-monitor.service"))
        self.assertNotIn(["systemctl", "disable", "--now", "demo-monitor.service"],
                         self.host.commands)
        self.assertTrue(self.host.exists(li.STATE_DIR))
        self.assertIn("shlab-mon", self.host.accounts)



class MissingUnitFileTests(InstallerTestCase):
    """Fix 1: never disable a different unit when our marked file is gone."""

    def test_foreign_unit_with_same_name_is_not_disabled(self):
        li.install(self.host, REPO)
        os.remove(self.host.p("/etc/systemd/system/demo-service.service"))
        # systemd now finds a *different* demo-service.service (e.g. from a package)
        self.host.unit_info["demo-service.service"] = {
            "LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0",
            "FragmentPath": "/usr/lib/systemd/system/demo-service.service"}
        rc = li.uninstall(self.host)
        self.assertEqual(rc, 1)  # finished, one item left in place
        self.assertNotIn(["systemctl", "disable", "--now", "demo-service.service"],
                         self.host.commands)
        self.assertNotIn(["systemctl", "reset-failed", "demo-service.service"],
                         self.host.commands)
        # our other unit was handled normally
        self.assertIn(["systemctl", "disable", "--now", "demo-monitor.service"],
                      self.host.commands)

    def test_running_foreign_unit_blocks_deletion(self):
        li.install(self.host, REPO)
        os.remove(self.host.p("/etc/systemd/system/demo-service.service"))
        self.host.unit_info["demo-service.service"] = {
            "LoadState": "loaded", "ActiveState": "active", "MainPID": "777",
            "FragmentPath": "/usr/lib/systemd/system/demo-service.service"}
        self.assertEqual(li.uninstall(self.host), 2)
        self.assertNotIn(["systemctl", "disable", "--now", "demo-service.service"],
                         self.host.commands)
        self.assertIn("shlab-demo", self.host.accounts)

    def test_missing_file_and_unit_unknown_is_just_dropped(self):
        li.install(self.host, REPO)
        os.remove(self.host.p("/etc/systemd/system/demo-service.service"))
        self.host.unit_info.pop("demo-service.service")  # systemd: not-found
        self.assertEqual(li.uninstall(self.host), 0)
        self.assertNotIn(["systemctl", "disable", "--now", "demo-service.service"],
                         self.host.commands)

    def test_missing_file_but_still_loaded_from_our_path_is_stopped(self):
        # deleted without daemon-reload: systemd still runs OUR unit
        li.install(self.host, REPO)
        os.remove(self.host.p("/etc/systemd/system/demo-service.service"))
        self.assertEqual(li.uninstall(self.host), 0)
        self.assertIn(["systemctl", "disable", "--now", "demo-service.service"],
                      self.host.commands)


class StopFailureTests(InstallerTestCase):
    """Fix 2: if a managed service cannot be stopped, delete nothing."""

    def assert_nothing_deleted(self):
        for path in li.MANAGED_DIRS:
            self.assertTrue(self.host.exists(path), path)
        for unit in li.UNITS:
            self.assertTrue(self.host.exists(os.path.join(li.UNIT_DIR, unit)), unit)
        self.assertEqual(set(self.host.accounts), set(li.ACCOUNTS))
        self.assertFalse([c for c in self.host.commands if c[0] in ("userdel", "groupdel")])
        self.assertTrue(self.host.exists(li.MANIFEST))

    def test_disable_command_fails(self):
        li.install(self.host, REPO)
        self.host.stop_fails["demo-monitor.service"] = "Job for demo-monitor.service canceled."
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = li.uninstall(self.host)
        self.assertEqual(rc, 2)
        self.assertIn("Job for demo-monitor.service canceled.", err.getvalue())
        self.assert_nothing_deleted()

    def test_service_still_running_after_stop(self):
        li.install(self.host, REPO)
        self.host.stop_hangs.add("demo-service.service")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = li.uninstall(self.host)
        self.assertEqual(rc, 2)
        self.assertIn("still running", err.getvalue())
        self.assert_nothing_deleted()

    def test_systemctl_unavailable_during_uninstall(self):
        li.install(self.host, REPO)
        self.host.query_error = subprocess.TimeoutExpired("systemctl", 15)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(li.uninstall(self.host), 2)
        self.assert_nothing_deleted()

    def test_retry_after_fixing_succeeds(self):
        li.install(self.host, REPO)
        self.host.stop_fails["demo-service.service"] = "boom"
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(li.uninstall(self.host), 2)
        del self.host.stop_fails["demo-service.service"]
        self.assertEqual(li.uninstall(self.host), 0)
        self.assertEqual(self.host.accounts, {})


class InterruptedInstallTests(InstallerTestCase):
    """Fix 4: interrupted installs are recorded and recoverable."""

    def interrupt_install(self, account="shlab-mon"):
        self.host.interrupt_after_useradd = account
        with self.assertRaises(KeyboardInterrupt):
            li.install(self.host, REPO)
        self.host.interrupt_after_useradd = None
        self.host.commands.clear()  # only count what happens after the crash

    def test_account_creation_intent_is_recorded_before_useradd(self):
        self.interrupt_install("shlab-mon")
        m = json.load(open(self.host.p(li.MANIFEST)))
        self.assertEqual(m["accounts"]["shlab-mon"], {"pending": True})
        self.assertIn("uid", m["accounts"]["shlab-demo"])  # completed before the crash

    def test_rerun_refuses_with_recovery_instructions(self):
        self.interrupt_install("shlab-mon")
        msg = self.assert_refused("creation of account 'shlab-mon' was interrupted")
        self.assertIn("sudo userdel shlab-mon", msg)

    def test_uninstall_leaves_pending_account_and_explains(self):
        self.interrupt_install("shlab-mon")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = li.uninstall(self.host)
        self.assertEqual(rc, 1)
        self.assertIn("shlab-mon", self.host.accounts)       # not auto-deleted
        self.assertNotIn("shlab-demo", self.host.accounts)   # recorded: removed
        self.assertIn("sudo userdel shlab-mon", out.getvalue())

    def test_manual_recovery_then_install_succeeds(self):
        self.interrupt_install("shlab-mon")
        self.host.run(["userdel", "shlab-mon"])  # the documented manual step
        li.install(self.host, REPO)
        m = json.load(open(self.host.p(li.MANIFEST)))
        self.assertIn("uid", m["accounts"]["shlab-mon"])

    def test_empty_etc_dir_without_manifest_gives_rmdir_hint(self):
        os.makedirs(self.host.p(li.ETC_DIR))
        self.assert_refused("sudo rmdir /etc/self-healing-lab")

    def test_leftover_temp_file_is_removed(self):
        tmp = li.tmp_path("/etc/systemd/system/demo-service.service")
        self.host.put(tmp, "half-written")
        li.install(self.host, REPO)
        self.assertFalse(self.host.exists(tmp))

    def test_symlinked_temp_file_is_refused_and_untouched(self):
        tmp = li.tmp_path("/etc/systemd/system/demo-service.service")
        os.symlink("/etc/passwd", self.host.p(tmp))
        self.assert_refused("is not a regular file")
        self.assertTrue(os.path.islink(self.host.p(tmp)))

    def test_write_never_follows_a_symlink_planted_after_preflight(self):
        # race: a symlink appears at the temp path between preflight and write
        victim = os.path.join(self.tmp, "victim.txt")
        with open(victim, "w") as f:
            f.write("original")
        target = "/etc/systemd/system/demo-service.service"
        os.symlink(victim, self.host.p(li.tmp_path(target)))
        with self.assertRaises(OSError):
            self.host.copy(os.path.join(REPO, "systemd", "demo-service.service"), target)
        self.assertEqual(open(victim).read(), "original")
        self.assertFalse(self.host.exists(target))

    def test_temp_names_are_fixed_and_project_specific(self):
        for t in li.known_temp_files():
            self.assertTrue(os.path.basename(t).startswith("."))
            self.assertTrue(t.endswith(".shlab-tmp"))

    def test_no_temp_files_left_after_install(self):
        li.install(self.host, REPO)
        for t in li.known_temp_files():
            self.assertFalse(self.host.exists(t), t)



class DashboardUpgradeTests(InstallerTestCase):
    """Step 3: upgrading an existing Step 2 install."""

    def make_step2_install(self):
        """Simulate the state left by the Step 2 installer (commit 5ce72f2)."""
        saved = (li.ACCOUNTS, li.UNITS)
        li.ACCOUNTS = {k: v for k, v in saved[0].items() if k != "shlab-dash"}
        li.UNITS = [u for u in saved[1] if u != "demo-dashboard.service"]
        try:
            li.install(self.host, REPO)
        finally:
            li.ACCOUNTS, li.UNITS = saved
        for name in li.STATE_FILES:
            p = self.host.p(os.path.join(li.STATE_DIR, name))
            with open(p, "w") as f:
                f.write("{}\n")
            os.chmod(p, 0o600)

    def test_upgrade_adds_dashboard_and_fixes_result_files(self):
        self.make_step2_install()
        self.host.commands.clear()
        li.install(self.host, REPO)
        m = json.load(open(self.host.p(li.MANIFEST)))
        self.assertIn("shlab-dash", m["accounts"])
        self.assertIn("demo-dashboard.service", m["units"])
        fixed = {os.path.basename(p) for p, *_ in self.host.fixed_files}
        self.assertEqual(fixed, set(li.STATE_FILES))
        for name in li.STATE_FILES:
            mode = os.stat(self.host.p(os.path.join(li.STATE_DIR, name))).st_mode & 0o777
            self.assertEqual(mode, 0o640, name)
            with open(self.host.p(os.path.join(li.STATE_DIR, name))) as f:
                self.assertEqual(f.read(), "{}\n")  # contents untouched

    def test_symlinked_result_file_is_refused(self):
        self.make_step2_install()
        p = self.host.p(os.path.join(li.STATE_DIR, "status.json"))
        os.remove(p)
        os.symlink("/etc/shadow", p)
        self.host.commands.clear()
        self.assert_refused("status.json is not a regular file")
        self.assertEqual(self.host.fixed_files, [])

    def test_uninstall_after_upgrade_removes_all_three(self):
        self.make_step2_install()
        li.install(self.host, REPO)
        self.assertEqual(li.uninstall(self.host), 0)
        self.assertEqual(self.host.accounts, {})
        self.assertIn(["systemctl", "disable", "--now", "demo-dashboard.service"],
                      self.host.commands)


if __name__ == "__main__":
    unittest.main()
