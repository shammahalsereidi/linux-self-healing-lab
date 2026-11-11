# Linux Self-Healing Lab

A small, safety-first lab that shows how a Linux service can **recover from failure by itself
using systemd**, and how to *measure* that honestly.

A demo HTTP service is crashed, killed or frozen on purpose. systemd restarts it when it
crashes; a monitor detects each incident, classifies it and measures the recovery; a local
dashboard shows what happened; and an experiment script makes the failures repeatable.

Python standard library only. Tested on an Ubuntu 24.04 laptop (bare metal).

## What it demonstrates

- **systemd self-healing:** `Restart=on-failure`, `RestartSec`, crash-loop protection with
  `StartLimitBurst`, and the difference between a failure and a planned stop.
- **Monitoring from systemd's own data:** `systemctl show` (state, PID, exit cause, restart
  counter), cgroup CPU and memory accounting, and systemd's monotonic timestamps.
- **Incident classification:** crash, hang, planned restart, and "uncertain" when the evidence
  is not enough. `/health` must report the same PID as systemd.
- **Honest measurement:** recovery time is reported as an interval with a stated starting
  point, never as a single exact number.
- **Least privilege:** one unprivileged account per service, systemd sandboxing, no access to
  the system journal, a dashboard that listens on 127.0.0.1 only and is read-only.
- **Safe operations:** an installer with preflight checks and a manifest, an uninstaller that
  stops services before deleting anything, and an experiment script with crash-loop and
  hang-resume safeguards.

## Architecture

```
                         systemd (PID 1)
   +--------------------------------------------------------------+
   |  demo-service.service      Restart=on-failure, RestartSec=2  |
   |    app/demo_service.py     127.0.0.1:8080   /health  /crash  |
   |          ^  restarts it after a crash                         |
   |          |                                                   |
   |  demo-monitor.service      every ~1 s: systemctl show        |
   |    monitor/monitor.py      + GET /health (PID must match)    |
   |          |  writes status.json, incidents.jsonl (0640)       |
   |          v                                                   |
   |  demo-dashboard.service    127.0.0.1:8081, read-only         |
   |    dashboard/dashboard.py                                    |
   +--------------------------------------------------------------+
          ^
          |  SIGKILL, POST /crash, or SIGSTOP + SIGCONT (demo-service only);
          |  results read through the dashboard's read-only API
   experiments/run_experiment.py   (run by you, from the Git clone)
```

## Verified results

All runs were on one HP laptop (bare metal, Ubuntu 24.04). **Each row is one individual run,
not an average.** They show that the mechanism works; a handful of runs is not a statistical
measurement of recovery time.

### Failures

| Run | Failure | How it was caused | Monitor result | PID | Detected after | Recovery interval (monitor) | Recovered by |
|---|---|---|---|---|---|---|---|
| 1 | SIGKILL | by hand: `systemctl kill -s SIGKILL` | failure | 10917 → 11278 | 0.632 s | 2.107–2.636 s | systemd automatic restart |
| 2 | crash (exit 1) | by hand: `curl -X POST .../crash` | failure, `exit code 1` | 11278 → 11446 | not recorded | 2.113–2.393 s | not recorded |
| 5 | SIGKILL | by hand, dashboard open | failure | 24901 → 26057 | 0.519 s | 2.119–2.522 s | systemd automatic restart |
| 6 | SIGKILL | experiment script: `kill --runs 1` | failure, `signal 9 (SIGKILL)` | 26057 → 30301 | 0.570 s | 2.054–2.572 s | systemd automatic restart |
| 7 | crash (exit 1) | experiment script: `crash --runs 1` | failure, `exit code 1` | 30301 → 30643 | 0.306 s | 2.056–2.308 s | systemd automatic restart |
| 8 | hang (process frozen) | experiment script: `hang --runs 1` | `unhealthy-but-running` | 30643 → 30643 | n/a | 3.001–4.099 s, **from the first failed check** | **the experiment script (`SIGCONT`), not systemd** |

The experiment script also measures recovery itself (`client_recovery_s`): 2.306 s (run 6),
2.356 s (run 7) and 0.001 s (run 8). It starts at a different moment from the monitor's
interval, so the two must not be compared directly (see below). All script checks passed in
runs 6-8. The new incident appeared on the dashboard in run 5; in runs 6-7 the dashboard showed
both incidents and returned to Healthy.

How to read these numbers:

- **Recovery interval** (crashes): the window in which that one recovery happened, measured
  from systemd's recorded exit of the old process. The monitor checks about once per second,
  so it gives a window, not an exact value. It is not a confidence interval.
- **About 2 s of every crash recovery is configuration**, not speed: `RestartSec=2` makes
  systemd wait 2 s before starting the new process.
- **Run 8 (hang):** the process was frozen with `SIGSTOP`, so it was alive and systemd's
  `Restart=` did **not** apply. **systemd did not recover it.** The monitor detected the
  problem (the process was still running but no longer answering `/health`), and the
  experiment script ended the hang by sending `SIGCONT`. The monitor's interval starts at the
  *first failed check*, not when the hang began, so **it can leave out the earlier part of the
  outage**. Here the process was stopped for about 5 s (the script's default `--hang-seconds`),
  while the interval is 3.0–4.1 s. Detection time is not given for a hang, because the monitor
  does not know when it started.
- **`client_recovery_s`** is measured by the experiment script, polling `/health` every 0.1 s.
  For `kill` it starts just before the script starts the `systemctl kill` command (so it
  includes that command's latency); for `crash`, just before the request is sent; for `hang`,
  after the `SIGCONT` command returned (so it measures how fast the process answers after
  resuming, not the outage).
  Details: [Two measurements with different starting points](docs/EXPERIMENTS.md#two-measurements-with-different-starting-points).
- "not recorded": the value was not noted for that run.

### Restarts that are not failures

| Run | Action | NRestarts | PID | Monitor result |
|---|---|---|---|---|
| 3 | `systemctl restart demo-service` | 2 → 0 | 11446 → 11683 | `uncertain`: likely manual |
| 4 | `systemctl stop`, then `start` | n/a | 11683 → 11877 | planned restart, no incident |

`NRestarts` counts only systemd's automatic restarts. In run 3 the manual restart did not
increase it (it reset it), which is the evidence the monitor uses to avoid calling a manual
restart a failure. Without a clean stop to observe, the monitor says "uncertain" rather than
guessing.

### First manual check

Before the monitor existed, a SIGKILL was checked by hand: `MainPID` 7985 → 8101, `NRestarts`
0 → 1, and `/health` then answered `{"status": "ok", "pid": 8101}`. Recovery time was not
measured.

## Limitations

- **Few runs, one machine.** One or two runs per scenario on one laptop. The numbers show the
  mechanism works; they say nothing about variation, other hardware or load.
- **About 1 s polling.** Recovery is reported as an interval whose width comes from the polling
  rate, and polls can be delayed by load. The incident records show the actual poll gaps.
- **Crash recovery time is mostly configuration.** About 2 s is the `RestartSec=2` delay.
- **Hangs are detected, not healed.** systemd's `Restart=` only reacts to a process that
  exits. Healing a hang would need, for example, `WatchdogSec=` with watchdog support in the
  app. That is not implemented.
- **Hang timing starts at detection.** For a hang, the interval can leave out the earlier part of
  the outage (see run 8).
- **Manual or failure?** The monitor uses systemd's `NRestarts` counter and observed states.
  When that is not enough, it records the event as "uncertain" instead of guessing.
- **systemd's start limit.** The experiment script spaces its own failures at least 20 s apart,
  but systemd does not expose its start-limit counter and other restarts also count, so the
  limit can still be reached. The script then stops and explains how to recover.
- **Lab scope.** A trivial demo service on localhost: no alerting, no remote access, no
  authentication (the dashboard is loopback-only and read-only), data in plain JSON files.
- **Platform.** Only Ubuntu 24.04 (systemd 255) was tested. The experiment script uses
  `--kill-who` on systemd older than 252, but no older system was tested. The code avoids
  syntax newer than Python 3.8, but it was run only with Ubuntu 24.04's `python3` and with
  Python 3.13.
- **Not separately verified:** the dashboard unit's kernel-level IP filter
  (`IPAddressDeny=any`, `IPAddressAllow=localhost`). It depends on the kernel's cgroup/BPF
  support. The application-level loopback-only bind and the `Host` header check are verified by
  tests.
- **Timings are not meaningful if the laptop suspends** during an incident.

## Quick start

Requirements: Linux with systemd, Python 3, `sudo`, free ports 8080 and 8081 on 127.0.0.1.
Details and safety notes: [docs/INSTALL.md](docs/INSTALL.md).

```bash
git clone <this repository> linux-self-healing-lab && cd linux-self-healing-lab
python3 -m unittest discover -s tests       # no systemd or root needed
sudo ./install.sh --dry-run                  # shows every change, makes none
sudo ./install.sh
```

Open **http://127.0.0.1:8081** on the same machine. Then, as your normal user:

```bash
python3 experiments/run_experiment.py kill --dry-run    # checks only, injects nothing
python3 experiments/run_experiment.py kill --runs 1
python3 experiments/run_experiment.py crash --runs 1
python3 experiments/run_experiment.py hang --runs 1
```

Remove everything again with `sudo ./uninstall.sh --dry-run`, then `sudo ./uninstall.sh`.
This **deletes all recorded results**; back them up first (see
[docs/INSTALL.md](docs/INSTALL.md#uninstall)).

## Demo scenario (about 3 minutes)

1. **Starting point.** Show the dashboard (Healthy, current PID) and
   `systemctl status demo-service`.
2. **The self-healing config.** Show `systemd/demo-service.service`: `Restart=on-failure`,
   `RestartSec=2`, `StartLimitBurst=5`, the unprivileged account and the sandbox options.
3. **Kill it.** `python3 experiments/run_experiment.py kill --runs 1`. On the dashboard: the
   status changes, a new incident appears with the PID change and the recovery interval, and
   the status returns to Healthy.
4. **Crash it.** `... crash --runs 1`: same result, cause "exit code 1".
5. **Freeze it.** `... hang --runs 1`: the process is alive but not answering. systemd does
   **not** restart it; the monitor detects it, and the script resumes it with `SIGCONT`. Point
   out that this interval starts at the first failed check.
6. **Planned maintenance.** `sudo systemctl stop demo-service`, wait, then `start`: the
   dashboard lists a planned restart, not a failure.
7. **Close with the limitations** above: few runs, 1 s polling, and hangs are not self-healed.

Recording tips: use a terminal prompt without your username or hostname (for example
`PS1='$ '`), and record only the browser tab with the dashboard.

## Documentation

| Topic | File |
|---|---|
| How systemd restarts the service, and what it does not restart | [docs/SYSTEMD.md](docs/SYSTEMD.md) |
| Monitor: incident rules, classification, how recovery is measured | [docs/MONITOR.md](docs/MONITOR.md) |
| Dashboard: what it shows, file permissions, network exposure | [docs/DASHBOARD.md](docs/DASHBOARD.md) |
| Failure experiments: running, stopping, crash-loop limit, hang safety | [docs/EXPERIMENTS.md](docs/EXPERIMENTS.md) |
| Install, upgrade, interrupted installs, uninstall | [docs/INSTALL.md](docs/INSTALL.md) |
| Security design | [docs/SECURITY.md](docs/SECURITY.md) |

## Project layout

```
app/demo_service.py             HTTP demo service (/health, /crash)
monitor/monitor.py              polls systemd + /health, detects incidents, measures recovery
dashboard/dashboard.py          read-only web dashboard on 127.0.0.1:8081 (+ static/ css, js)
experiments/run_experiment.py   controlled failure experiments (demo-service only)
installer/lab_install.py        safe install/uninstall with preflight checks and a manifest
systemd/*.service               units for the demo service, monitor and dashboard
tests/                          unit tests (no systemd or root needed)
install.sh / uninstall.sh       thin wrappers around installer/lab_install.py
docs/                           detailed documentation
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

The tests use fake systemd, process and network layers, so they run without root and without
changing the system. They cover the monitor's classification and timing rules, the
installer's collision and uninstall rules, the dashboard's binding, request checks and escaping,
and the experiment script's safety rules (targeting, crash-loop spacing, hang resume
verification). On the laptop, the suite passed at commit `8b3abf7` (134 tests, before runs 6-7).
It has grown since; run it yourself for the current count.

## How it was built

The project was built in steps, each reviewed and tested on the laptop before the next:

1. Demo service under systemd with automatic restart
2. Monitor: status, CPU, memory, logs, incident detection, recovery measurement
3. Dashboard on 127.0.0.1
4. Repeatable controlled-failure experiments
5. Results, limitations and demo scenario (this README)

Some documents and error messages still refer to these step numbers (for example "Upgrading
from Step 1").
