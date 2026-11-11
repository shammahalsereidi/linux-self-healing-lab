#!/usr/bin/env python3
"""Safe installer / uninstaller for the Linux Self-Healing Lab.

Safety rules:
  * Preflight first. Every account, group, unit and path is checked BEFORE
    anything is changed. If any check fails, nothing is modified.
  * Never adopt something we did not create. An existing account, group,
    unit file or directory is only reused if the install manifest says this
    installer created it (and, for accounts, the UID and comment still match).
  * Uninstall removes only what the manifest lists, and re-verifies each item
    (UID + comment for accounts, marker line + systemd FragmentPath for units)
    before acting on it. Anything that no longer matches is left alone.
  * Uninstall runs in two phases: first stop every managed service and verify
    it stopped; only if that fully succeeds, delete files and accounts.
  * Intent is recorded before action: directories, units and accounts are
    written to the manifest *before* they are created, so an interrupted
    install never leaves an unrecorded account behind.

Uninstall exit codes: 0 = everything removed, 1 = finished but some items
were left in place (listed), 2 = aborted before deleting anything.

The manifest lives in /etc/self-healing-lab/manifest.json (root-owned).

Usage:
  sudo python3 installer/lab_install.py install   [--dry-run]
  sudo python3 installer/lab_install.py uninstall [--dry-run]
"""

import argparse
import grp
import json
import os
import pwd
import shutil
import stat
import subprocess
import sys
from typing import Dict, List, Optional

MARKER = "# Managed by linux-self-healing-lab (installer/lab_install.py)."

# account name -> GECOS comment (used to recognise our own accounts)
ACCOUNTS = {
    "shlab-demo": "Linux Self-Healing Lab demo service",
    "shlab-mon": "Linux Self-Healing Lab monitor",
    "shlab-dash": "Linux Self-Healing Lab dashboard",
}
UNITS = ["demo-service.service", "demo-monitor.service", "demo-dashboard.service"]
UNIT_DIR = "/etc/systemd/system"
ETC_DIR = "/etc/self-healing-lab"
MANIFEST = ETC_DIR + "/manifest.json"
OPT_DIR = "/opt/self-healing-lab"
STATE_DIR = "/var/lib/self-healing-lab"
LOG_DIR = "/var/log/self-healing-lab"
MANAGED_DIRS = [ETC_DIR, OPT_DIR, STATE_DIR, LOG_DIR]
CODE_FILES = ["app/demo_service.py", "monitor/monitor.py", "dashboard/dashboard.py",
              "dashboard/static/dashboard.css", "dashboard/static/dashboard.js"]
# Monitor output files whose owner/mode the installer keeps correct
# (an older install wrote them 0600 shlab-mon:shlab-mon).
STATE_FILES = ["status.json", "incidents.jsonl"]
STATE_FILE_SPEC = ("shlab-mon", "shlab-dash", 0o640)

# Directory ownership and modes:  path -> (owner, group, mode)
DIR_SPEC = {
    ETC_DIR: ("root", "root", 0o755),
    OPT_DIR: ("root", "root", 0o755),
    STATE_DIR: ("shlab-mon", "shlab-dash", 0o2750),  # monitor writes, dashboard reads
    LOG_DIR: ("shlab-demo", "shlab-mon", 0o2750),   # demo writes, monitor reads
}

# What uninstall deletes from each directory (printed, and in docs/INSTALL.md).
DIR_CONTENTS = {
    OPT_DIR: "installed code",
    STATE_DIR: "monitor results: status.json, incidents.jsonl",  # noqa
    LOG_DIR: "demo service log files",
}

TMP_SUFFIX = ".shlab-tmp"
SYSTEMCTL_TIMEOUT = 15


def tmp_path(path: str) -> str:
    """Fixed, project-specific temp name next to the target: .<name>.shlab-tmp"""
    d, name = os.path.split(path)
    return os.path.join(d, "." + name + TMP_SUFFIX)


def known_temp_files() -> List[str]:
    """Every temp file this installer can ever create (exact paths, no globbing)."""
    targets = [MANIFEST] + [os.path.join(UNIT_DIR, u) for u in UNITS] + \
              [os.path.join(OPT_DIR, rel) for rel in CODE_FILES]
    return [tmp_path(t) for t in targets]


class InstallError(Exception):
    pass


# --------------------------------------------------------------------------
# Host: every interaction with the real system goes through this class, so
# tests can substitute a fake one.
# --------------------------------------------------------------------------

class Host:
    def __init__(self, root: str = "/", dry_run: bool = False):
        self.root = root
        self.dry_run = dry_run
        self.owner_uid = os.geteuid()  # owner of temp files we create (root in real use)

    def p(self, path: str) -> str:
        """Map an absolute target path to the real path (root prefix for tests)."""
        return os.path.join(self.root, path.lstrip("/"))

    # -- read-only queries -------------------------------------------------
    def account(self, name: str) -> Optional[dict]:
        try:
            pw = pwd.getpwnam(name)
        except KeyError:
            return None
        return {"uid": pw.pw_uid, "gid": pw.pw_gid, "gecos": pw.pw_gecos}

    def group(self, name: str) -> Optional[dict]:
        try:
            gr = grp.getgrnam(name)
        except KeyError:
            return None
        return {"gid": gr.gr_gid, "members": list(gr.gr_mem)}

    def query(self, cmd: List[str]) -> subprocess.CompletedProcess:
        """Run a read-only command (executed even in dry-run)."""
        return subprocess.run(cmd, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT)

    # -- mutations (skipped in dry-run) ------------------------------------
    def say(self, msg: str):
        print(("[dry-run] " if self.dry_run else "") + msg)

    def run(self, cmd: List[str], check: bool = True) -> Optional[subprocess.CompletedProcess]:
        self.say("run: " + " ".join(cmd))
        if self.dry_run:
            return None
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=SYSTEMCTL_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as exc:
            if check:
                raise InstallError("command failed: %s: %s" % (" ".join(cmd), exc))
            # Report it like a failed command so callers see the reason.
            return subprocess.CompletedProcess(cmd, -1, "", str(exc))
        if check and res.returncode != 0:
            raise InstallError("command failed (%d): %s\n%s"
                               % (res.returncode, " ".join(cmd), res.stderr.strip()))
        return res

    def makedir(self, path: str, owner: str, group: str, mode: int):
        self.say("mkdir %s (%s:%s %o)" % (path, owner, group, mode))
        if self.dry_run:
            return
        real = self.p(path)
        os.makedirs(real, exist_ok=True)
        self.chown(real, owner, group)
        os.chmod(real, mode)

    def fix_file(self, path: str, owner: str, group: str, mode: int):
        """Set owner/group/mode of an existing regular file, never via a symlink."""
        self.say("chown %s:%s, chmod %o %s" % (owner, group, mode, path))
        if self.dry_run:
            return
        fd = os.open(self.p(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise InstallError("%s is not a regular file" % path)
            os.fchown(fd, pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)
            os.fchmod(fd, mode)
        finally:
            os.close(fd)

    def chown(self, real_path: str, owner: str, group: str):
        os.chown(real_path, pwd.getpwnam(owner).pw_uid, grp.getgrnam(group).gr_gid)

    def _write_atomic(self, dst: str, data: bytes, mode: int):
        """Write to the fixed temp name, then rename over the target.

        O_EXCL|O_NOFOLLOW: never write through a symlink or into a file that
        is already there (leftovers are removed by remove_temp_files first).
        """
        real, tmp = self.p(dst), self.p(tmp_path(dst))
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, real)
        except BaseException:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            raise

    def copy(self, src: str, dst: str, mode: int = 0o644):
        self.say("install %s -> %s (%o)" % (src, dst, mode))
        if self.dry_run:
            return
        with open(src, "rb") as f:
            self._write_atomic(dst, f.read(), mode)

    def remove_file(self, path: str):
        self.say("rm %s" % path)
        if not self.dry_run:
            os.remove(self.p(path))

    def remove_tree(self, path: str):
        self.say("rm -r %s" % path)
        if not self.dry_run:
            shutil.rmtree(self.p(path))

    def write_text(self, path: str, text: str, mode: int = 0o644):
        if self.dry_run:
            return
        self._write_atomic(path, text.encode(), mode)


# --------------------------------------------------------------------------
# Manifest: the record of what this installer created
# --------------------------------------------------------------------------

class Manifest:
    def __init__(self, host: Host, data: Optional[dict] = None, exists: bool = False):
        self.host = host
        self.exists = exists
        self.data = data or {"version": 1, "accounts": {}, "dirs": [], "units": []}

    @classmethod
    def load(cls, host: Host) -> "Manifest":
        real = host.p(MANIFEST)
        if os.path.islink(real):
            raise InstallError("%s is a symlink; refusing to use it" % MANIFEST)
        if not os.path.exists(real):
            return cls(host)
        try:
            with open(real) as f:
                data = json.load(f)
            for key, typ in (("accounts", dict), ("dirs", list), ("units", list)):
                if not isinstance(data.get(key), typ):
                    raise ValueError("missing or invalid '%s'" % key)
        except (OSError, ValueError) as exc:
            raise InstallError("cannot read manifest %s: %s" % (MANIFEST, exc))
        return cls(host, data, exists=True)

    @property
    def accounts(self) -> Dict[str, dict]:
        return self.data["accounts"]

    def has_dir(self, path):
        return path in self.data["dirs"]

    def has_unit(self, unit):
        return unit in self.data["units"]

    def add(self, key, value):
        if value not in self.data[key]:
            self.data[key].append(value)
        self.save()

    def discard(self, key, value):
        if key == "accounts":
            self.data[key].pop(value, None)
        elif value in self.data[key]:
            self.data[key].remove(value)
        self.save()

    def save(self):
        self.host.write_text(MANIFEST, json.dumps(self.data, indent=2) + "\n")
        self.exists = True


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def unit_has_marker(host: Host, unit: str) -> bool:
    try:
        with open(host.p(os.path.join(UNIT_DIR, unit))) as f:
            return f.readline().startswith(MARKER)
    except OSError:
        return False


UNIT_PROPS = ("LoadState", "FragmentPath", "DropInPaths", "ActiveState", "MainPID")


def systemctl_unit_info(host: Host, unit: str) -> Dict[str, str]:
    """What systemd knows about a unit, or raise InstallError."""
    cmd = ["systemctl", "show", unit, "--property=" + ",".join(UNIT_PROPS)]
    try:
        res = host.query(cmd)
    except FileNotFoundError:
        raise InstallError("systemctl not found - is this a systemd system?")
    except subprocess.TimeoutExpired:
        raise InstallError("systemctl timed out while checking %s" % unit)
    except OSError as exc:
        raise InstallError("could not run systemctl: %s" % exc)
    if res.returncode != 0:
        raise InstallError("systemctl show %s failed (%d): %s"
                           % (unit, res.returncode, res.stderr.strip()))
    info = {}
    for line in res.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            info[key] = value
    if "LoadState" not in info:
        raise InstallError("unexpected systemctl output for %s" % unit)
    return info


def check_temp_files(host: Host) -> List[str]:
    """Leftover temp files must be plain files we own; anything else is suspicious."""
    problems = []
    for t in known_temp_files():
        real = host.p(t)
        if not os.path.lexists(real):
            continue
        st = os.lstat(real)
        if os.path.islink(real) or not os.path.isfile(real) or st.st_uid != host.owner_uid:
            problems.append("%s exists but is not a regular file owned by uid %d; "
                            "refusing to touch it" % (t, host.owner_uid))
    return problems


def remove_temp_files(host: Host):
    """Remove leftovers of an interrupted run (only plain files we own)."""
    for t in known_temp_files():
        real = host.p(t)
        if os.path.isfile(real) and not os.path.islink(real) \
                and os.lstat(real).st_uid == host.owner_uid:
            host.say("removing leftover temp file %s" % t)
            if not host.dry_run:
                os.unlink(real)


INTERRUPTED_ACCOUNT_HELP = (
    "creation of account '%s' was interrupted in an earlier install (the manifest "
    "recorded the intent but not the result). It now exists (uid %d, comment '%s'). "
    "The installer will not adopt or delete it automatically. If it is the one from "
    "that run (comment 'Linux Self-Healing Lab ...', shell /usr/sbin/nologin, no "
    "processes: `ps -u %s`), remove it with `sudo userdel %s` and run the installer "
    "again. See docs/INSTALL.md, 'Recovering from an interrupted install'.")


def preflight_install(host: Host, manifest: Manifest, repo: str) -> List[str]:
    problems = check_temp_files(host)

    # Source files must exist, and units must carry our marker.
    for rel in CODE_FILES:
        if not os.path.isfile(os.path.join(repo, rel)):
            problems.append("source file missing: %s" % rel)
    for unit in UNITS:
        src = os.path.join(repo, "systemd", unit)
        try:
            with open(src) as f:
                if not f.readline().startswith(MARKER):
                    problems.append("source unit %s lacks the managed-by marker" % unit)
        except OSError:
            problems.append("source unit missing: systemd/%s" % unit)

    # Accounts and their groups.
    for name, gecos in ACCOUNTS.items():
        acc, grp_ = host.account(name), host.group(name)
        rec = manifest.accounts.get(name)
        if acc:
            if not rec:
                problems.append("account '%s' already exists (uid %d) and was not created by "
                                "this installer; refusing to reuse it" % (name, acc["uid"]))
            elif rec.get("pending"):
                problems.append(INTERRUPTED_ACCOUNT_HELP
                                % (name, acc["uid"], acc["gecos"], name, name))
            elif acc["uid"] != rec.get("uid") or acc["gecos"] != gecos:
                problems.append("account '%s' no longer matches the one this installer created "
                                "(uid %s, expected %s); refusing to reuse it"
                                % (name, acc["uid"], rec.get("uid")))
        else:
            if rec and not rec.get("pending"):
                problems.append("manifest lists account '%s' but it does not exist; "
                                "run uninstall first to clean up" % name)
            if grp_:
                hint = (" (possibly left by the interrupted creation of account '%s'; if "
                        "it has no members, remove it with `sudo groupdel %s`)" % (name, name)
                        if rec and rec.get("pending") else "")
                problems.append("group '%s' already exists (gid %d) without a matching "
                                "account; refusing to reuse it%s" % (name, grp_["gid"], hint))

    # Directories.
    for path in MANAGED_DIRS:
        real = host.p(path)
        if os.path.islink(real):
            problems.append("%s is a symlink; refusing to touch it" % path)
        elif os.path.exists(real) and not manifest.has_dir(path):
            if path == ETC_DIR and not manifest.exists:
                problems.append("%s exists but has no manifest; refusing to touch it. If it "
                                "is empty (an install interrupted at its first step), remove "
                                "it with `sudo rmdir %s`" % (path, path))
            elif path != ETC_DIR:
                problems.append("%s already exists and was not created by this installer"
                                % path)

    for name in STATE_FILES:
        real = host.p(os.path.join(STATE_DIR, name))
        if os.path.lexists(real) and (os.path.islink(real) or not os.path.isfile(real)):
            problems.append("%s/%s is not a regular file; refusing to touch it"
                            % (STATE_DIR, name))

    if not os.path.isdir(host.p(UNIT_DIR)):
        problems.append("%s does not exist - is this a systemd system?" % UNIT_DIR)

    # Units: our file in /etc/systemd/system, and what systemd itself sees.
    for unit in UNITS:
        target = os.path.join(UNIT_DIR, unit)
        real = host.p(target)
        if os.path.islink(real):
            problems.append("%s is a symlink; refusing to overwrite it" % target)
            continue
        if os.path.exists(real) and not (manifest.has_unit(unit) and unit_has_marker(host, unit)):
            problems.append("%s already exists and is not managed by this installer" % target)
        try:
            info = systemctl_unit_info(host, unit)
        except InstallError as exc:
            problems.append(str(exc))
            continue
        load, frag = info.get("LoadState"), info.get("FragmentPath", "")
        if load == "masked":
            problems.append("%s is masked; refusing to change it" % unit)
        elif load != "not-found" and frag and frag != target:
            problems.append("%s is already defined by %s; refusing to shadow it" % (unit, frag))
        if info.get("DropInPaths", "").strip():
            problems.append("%s has drop-in overrides (%s) that would change its behaviour; "
                            "remove them first" % (unit, info["DropInPaths"]))
    return problems


# --------------------------------------------------------------------------
# Install / uninstall
# --------------------------------------------------------------------------

LEGACY_HINT = """
Hint: if this machine has the Step 1 version of this lab installed (it used
DynamicUser and had no manifest), remove it with the Step 1 uninstaller first.
See docs/INSTALL.md, section "Upgrading from Step 1"."""


def install(host: Host, repo: str):
    manifest = Manifest.load(host)
    problems = preflight_install(host, manifest, repo)
    if problems:
        raise InstallError("preflight failed, nothing was changed:\n  - "
                           + "\n  - ".join(problems) + LEGACY_HINT)
    host.say("preflight OK")
    remove_temp_files(host)

    # 1. Manifest directory first, so every later step can be recorded.
    host.makedir(ETC_DIR, *DIR_SPEC[ETC_DIR])
    manifest.add("dirs", ETC_DIR)

    # 2. Accounts. Intent is saved first ("pending"), so an interruption right
    #    after useradd still leaves a record of it.
    for name, gecos in ACCOUNTS.items():
        if host.account(name) is None:
            manifest.accounts[name] = {"pending": True}
            manifest.save()
            host.run(["useradd", "--system", "--user-group", "--no-create-home",
                      "--home-dir", "/nonexistent", "--shell", "/usr/sbin/nologin",
                      "--comment", gecos, name])
            acc = host.account(name)
            if acc is not None:  # None only in dry-run
                manifest.accounts[name] = {"uid": acc["uid"], "gid": acc["gid"]}
                manifest.save()

    # 3. Directories (recorded *before* creation, so a crash leaves no orphan).
    for path in (OPT_DIR, STATE_DIR, LOG_DIR):
        manifest.add("dirs", path)
        host.makedir(path, *DIR_SPEC[path])
    for rel in CODE_FILES:
        parts = os.path.dirname(rel).split("/")
        for i in range(1, len(parts) + 1):   # each level explicitly root 0755
            host.makedir(os.path.join(OPT_DIR, *parts[:i]), "root", "root", 0o755)
        host.copy(os.path.join(repo, rel), os.path.join(OPT_DIR, rel))
    # Results written by an older version (0600, group shlab-mon) must become
    # readable by the dashboard's group. Contents are not changed.
    for name in STATE_FILES:
        path = os.path.join(STATE_DIR, name)
        if os.path.isfile(host.p(path)):
            host.fix_file(path, *STATE_FILE_SPEC)

    # 4. Units.
    for unit in UNITS:
        manifest.add("units", unit)
        host.copy(os.path.join(repo, "systemd", unit), os.path.join(UNIT_DIR, unit))
    host.run(["systemctl", "daemon-reload"])
    for unit in UNITS:
        host.run(["systemctl", "enable", unit])
        host.run(["systemctl", "restart", unit])  # restart so updates take effect
    host.say("install complete")


def _is_our_unit_file(host: Host, unit: str) -> bool:
    real = host.p(os.path.join(UNIT_DIR, unit))
    return os.path.isfile(real) and not os.path.islink(real) and unit_has_marker(host, unit)


def _is_running(info: Dict[str, str]) -> bool:
    return info.get("ActiveState") not in ("inactive", "failed") or \
        info.get("MainPID", "0") not in ("0", "")


def uninstall(host: Host):
    manifest = Manifest.load(host)
    if not manifest.exists:
        raise InstallError("no manifest at %s: nothing installed by this installer was found, "
                           "so nothing was changed.%s" % (MANIFEST, LEGACY_HINT))
    skipped: List[str] = []
    errors: List[str] = []
    removable_units: List[str] = []

    # ---- Phase 1: stop and disable our services, and verify they stopped. ----
    for unit in list(manifest.data["units"]):
        target = os.path.join(UNIT_DIR, unit)
        try:
            info = systemctl_unit_info(host, unit)
        except InstallError as exc:
            errors.append(str(exc))
            continue
        load, frag = info.get("LoadState"), info.get("FragmentPath", "")
        ours_on_disk = _is_our_unit_file(host, unit)

        if os.path.lexists(host.p(target)) and not ours_on_disk:
            skipped.append("%s no longer carries our marker; not stopped, not removed" % target)
        elif load not in ("not-found", None) and frag != target:
            # Our file is gone and systemd now loads this name from somewhere else:
            # a different unit. Never stop or disable it.
            skipped.append("%s is now defined by %s (not by this installer); not stopped "
                           "or disabled" % (unit, frag or "an unknown path"))
        elif load == "not-found":
            removable_units.append(unit)       # nothing loaded, nothing to stop
            continue
        else:
            # Ours: our marked file, or systemd still has it loaded from our path.
            res = host.run(["systemctl", "disable", "--now", unit], check=False)
            if host.dry_run:
                removable_units.append(unit)
                continue
            if res is None or res.returncode != 0:
                detail = (res.stderr.strip() if res is not None else "") or "no details"
                errors.append("stopping/disabling %s failed: %s" % (unit, detail))
                continue
            try:
                after = systemctl_unit_info(host, unit)
            except InstallError as exc:
                errors.append("could not verify that %s stopped: %s" % (unit, exc))
                continue
            if _is_running(after):
                errors.append("%s is still running after stop (ActiveState=%s, MainPID=%s)"
                              % (unit, after.get("ActiveState"), after.get("MainPID")))
                continue
            removable_units.append(unit)
            continue
        # A unit we are leaving alone must not be running, since it might use our
        # accounts or directories.
        if _is_running(info):
            errors.append("%s is running and is not ours to stop; not deleting accounts or "
                          "directories it might use" % unit)

    if errors:
        print("Uninstall aborted before deleting anything (manifest kept):", file=sys.stderr)
        for e in errors:
            print("  - " + e, file=sys.stderr)
        for s_ in skipped:
            print("  - (left in place) " + s_, file=sys.stderr)
        print("Fix the problem (e.g. `systemctl status <unit>`) and run uninstall again.",
              file=sys.stderr)
        return 2

    # ---- Phase 2: everything is stopped; delete. ----
    remove_temp_files(host)
    for unit in removable_units:
        host.run(["systemctl", "reset-failed", unit], check=False)
        if _is_our_unit_file(host, unit):
            host.remove_file(os.path.join(UNIT_DIR, unit))
        manifest.discard("units", unit)
    host.run(["systemctl", "daemon-reload"], check=False)

    for path in [d for d in manifest.data["dirs"] if d != ETC_DIR]:
        if path not in MANAGED_DIRS:
            skipped.append("%s: unexpected path in manifest; left in place" % path)
            continue
        real = host.p(path)
        if os.path.islink(real):
            skipped.append("%s: is a symlink; left in place" % path)
            continue
        if os.path.exists(real):
            host.say("deleting %s and ALL its contents (%s)" % (path, DIR_CONTENTS.get(path, "")))
            host.remove_tree(path)
        manifest.discard("dirs", path)

    for name in list(manifest.accounts):
        rec = manifest.accounts[name]
        acc, grp_ = host.account(name), host.group(name)
        if name not in ACCOUNTS:
            skipped.append("account '%s': not one of ours; left in place" % name)
            continue
        if rec.get("pending"):
            # Creation was interrupted: we cannot prove an existing account is ours.
            if acc:
                skipped.append(INTERRUPTED_ACCOUNT_HELP
                               % (name, acc["uid"], acc["gecos"], name, name))
            elif grp_:
                skipped.append("group '%s' may be left by an interrupted account creation; "
                               "if it has no members, remove it with `sudo groupdel %s`"
                               % (name, name))
            else:
                manifest.discard("accounts", name)
            continue
        if acc is None:
            manifest.discard("accounts", name)
            continue
        if acc["uid"] != rec.get("uid") or acc["gecos"] != ACCOUNTS[name]:
            skipped.append("account '%s': uid/comment changed since install; left in place"
                           % name)
            continue
        try:
            host.run(["userdel", name])
        except InstallError as exc:
            skipped.append("account '%s': userdel failed, left in place: %s" % (name, exc))
            continue
        # userdel normally removes the user's private group too; clean up if not.
        g = host.group(name)
        if g and g["gid"] == rec.get("gid") and not g["members"]:
            host.run(["groupdel", name], check=False)
        manifest.discard("accounts", name)

    if skipped:
        print("Uninstall finished with items left in place (manifest kept):")
        for s_ in skipped:
            print("  - " + s_)
        return 1
    host.remove_tree(ETC_DIR)
    host.say("uninstall complete")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Linux Self-Healing Lab installer")
    ap.add_argument("action", choices=["install", "uninstall"])
    ap.add_argument("--dry-run", action="store_true", help="show what would change")
    ap.add_argument("--root", default="/", help=argparse.SUPPRESS)  # tests only
    args = ap.parse_args(argv)

    if os.geteuid() != 0 and not args.dry_run:
        print("Please run as root (or use --dry-run).", file=sys.stderr)
        return 1
    host = Host(root=args.root, dry_run=args.dry_run)
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        if args.action == "install":
            install(host, repo)
            return 0
        return uninstall(host)
    except InstallError as exc:
        print("ERROR: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
