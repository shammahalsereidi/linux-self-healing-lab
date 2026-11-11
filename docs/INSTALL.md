# Installation

## Requirements

- Linux with systemd as PID 1 (tested on Ubuntu 24.04; see [Limitations](../README.md#limitations))
- Python 3, standard library only (nothing to pip install). Run with the `python3` of Ubuntu
  24.04 and with Python 3.13; the code avoids syntax newer than 3.8, but 3.8 itself was not tested
- `curl`, `sudo`
- free ports 8080 (demo service) and 8081 (dashboard) on 127.0.0.1

## Install

```bash
sudo ./install.sh --dry-run   # show what would change
sudo ./install.sh
curl http://127.0.0.1:8080/health
```

Run `sudo ./install.sh` again after pulling new code; it redeploys and restarts all units.

## What the installer creates

| Item | Owner / mode | Purpose |
|---|---|---|
| accounts `shlab-demo`, `shlab-mon`, `shlab-dash` | system accounts, no login shell, no home | run the three services |
| `/opt/self-healing-lab/` | root, 0755 | code |
| `/etc/systemd/system/demo-service.service`, `demo-monitor.service`, `demo-dashboard.service` | root, 0644 | units |
| `/var/log/self-healing-lab/` | `shlab-demo:shlab-mon`, 2750 | demo writes its log, only the monitor can read it |
| `/var/lib/self-healing-lab/` | `shlab-mon:shlab-dash`, 2750 (files 0640) | monitor writes results, only the dashboard can read them |
| `/etc/self-healing-lab/manifest.json` | root, 0644 | record of what was created |

## Installer safety rules

1. **Preflight before any change.** All accounts, groups, unit names and paths are checked
   first; if anything fails, all problems are listed and **nothing is modified**.
2. **Never adopt foreign resources.** An existing account, group, directory or unit file is
   only reused if the manifest says this installer created it (accounts must also still have
   the recorded UID and comment). The installer also asks systemd whether a unit with the same
   name is already defined elsewhere (e.g. by a package), is masked, or has drop-in overrides.
3. **Uninstall removes only what the manifest lists**, and re-verifies each item first:
   accounts by UID + comment, unit files by their `# Managed by linux-self-healing-lab` first
   line *and* by asking systemd where the unit is loaded from. If our unit file is gone and
   systemd now finds a unit with the same name elsewhere, that unit is never stopped or
   disabled. Anything that no longer matches is left in place and reported.
4. **Uninstall stops first, deletes second.** Every managed service is stopped and verified
   stopped (`ActiveState` inactive/failed, `MainPID=0`). If any stop fails, or a unit we must
   leave alone is still running, uninstall reports why and exits with code 2 **without
   deleting any file, directory or account**. Fix the cause and run it again.
5. **Intent is recorded before action.** Directories, units and accounts are written to the
   manifest *before* they are created, so an interruption cannot leave an unrecorded account.
   Temporary files use fixed names (`.<file>.shlab-tmp`) and are never written through a
   symlink.

Exit codes: `0` success; `1` (uninstall) finished but some items were left in place and listed;
`1` (install) preflight refused; `2` (uninstall) aborted before deleting anything.

## Recovering from an interrupted install

If an install is interrupted (Ctrl+C, power loss, a failing command), **run
`sudo ./install.sh --dry-run` first**: preflight lists exactly what it found. In most cases
simply running `sudo ./install.sh` again completes the install, because everything already
created is recorded in the manifest and is reused. Leftover temporary files at the installer's
own fixed names are removed automatically (only if they are regular files owned by root;
anything else at those names, e.g. a symlink, makes preflight refuse).

Two situations need one manual step, because the installer cannot prove the leftover is its own:

| Preflight message | Cause | What to do |
|---|---|---|
| `creation of account 'shlab-…' was interrupted` | the interruption hit right after `useradd`; the manifest has a `pending` record for that account | check it is the lab's account: `getent passwd shlab-mon` shows the comment `Linux Self-Healing Lab …` and shell `/usr/sbin/nologin`, and `ps -u shlab-mon` shows no processes. Then `sudo userdel shlab-mon` and re-run the installer. |
| `group 'shlab-…' already exists … interrupted` | `useradd` created the group but not the account | `getent group shlab-mon` shows no members, then `sudo groupdel shlab-mon` and re-run. |
| `/etc/self-healing-lab exists but has no manifest` | interrupted at the very first step | if `sudo ls -A /etc/self-healing-lab` prints nothing, `sudo rmdir /etc/self-healing-lab` (rmdir only removes empty directories). |

Alternatively, `sudo ./uninstall.sh` removes everything the manifest recorded; it leaves a
`pending` account in place and prints the same instructions.

## Upgrading from Step 1

"Step 1" is the project's first version (commit `5922051`), which had
only the demo service. Its installer had no manifest, so the new installer will (correctly) refuse to touch
the Step 1 files. Remove Step 1 with its own uninstaller from Git history first. It only
deletes `/etc/systemd/system/demo-service.service` and `/opt/self-healing-lab`:

```bash
git show 5922051:uninstall.sh > ~/step1-uninstall.sh
less ~/step1-uninstall.sh
sudo bash ~/step1-uninstall.sh
sudo ./install.sh
```


## Uninstall

> **Warning: uninstall deletes all recorded results.** It removes these directories
> completely, including everything in them:
>
> | Directory | Contents that are deleted |
> |---|---|
> | `/var/lib/self-healing-lab/` | monitor results: `status.json`, `incidents.jsonl` (all recorded incidents and recovery times) |
> | `/var/log/self-healing-lab/` | demo service log files (`demo.log`, `demo.log.1`, `demo.log.2`) |
> | `/opt/self-healing-lab/` | installed code (the copy in your Git clone is not touched) |
>
> It also removes `/etc/self-healing-lab/` (the manifest), the three unit files and the
> three accounts. It does not touch your Git clone, your home directory, or the system journal.

Back up results first if you want to keep them:

```bash
sudo tar czf ~/shlab-results-$(date +%Y%m%d-%H%M%S).tar.gz -C / var/lib/self-healing-lab var/log/self-healing-lab
sudo chown "$USER": ~/shlab-results-*.tar.gz
```

Then review and uninstall:

```bash
sudo ./uninstall.sh --dry-run   # lists every directory, unit and account it would remove
sudo ./uninstall.sh
```
