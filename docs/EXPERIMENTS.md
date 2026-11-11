# Controlled failure experiments

`experiments/run_experiment.py` injects a failure into the demo service, waits for the
monitor to record the incident, checks the result against what should happen, and saves it.
It runs from your Git clone; nothing is installed.

| Scenario | What it does | Expected incident |
|---|---|---|
| `kill` | `systemctl kill --kill-whom=main --signal=SIGKILL demo-service.service` | `failure`, cause `signal 9 (SIGKILL)`, new PID, systemd automatic restart |
| `crash` | `POST http://127.0.0.1:8080/crash` (the app exits with code 1) | `failure`, cause `exit code 1`, new PID, systemd automatic restart |
| `hang` | `SIGSTOP` the main process for N s (default 5), then `SIGCONT` | `unhealthy-but-running`, same PID, measured from the first failed check |

The hang is a useful contrast: the process is alive but frozen, so **systemd does not restart
it**. The monitor detects it; recovery only happens because the experiment resumes it.

## Requirements

The lab must be installed and running (see [INSTALL.md](INSTALL.md)): `demo-service`, `demo-monitor` and
`demo-dashboard` active, monitor status `healthy`, no open incident. Run as **your normal
user**: `kill` and `hang` call `sudo systemctl kill ...` (you will be asked for your password
once at the start); `crash` needs no privileges.

## Run

Check the preconditions first (injects nothing):

```bash
python3 experiments/run_experiment.py kill --dry-run
```

Then one scenario at a time:

```bash
python3 experiments/run_experiment.py kill --runs 3
python3 experiments/run_experiment.py crash --runs 3
python3 experiments/run_experiment.py hang --runs 3 --hang-seconds 5
```

Keep the dashboard open to watch incidents appear. Each run:

1. checks the preconditions above and that `/health` reports systemd's `MainPID`;
2. waits until the service has been up for at least 20 s (see below);
3. injects the failure;
4. polls `/health` every 0.1 s as a script-side cross-check (`client_recovery_s`, see
   [Two measurements](#two-measurements-with-different-starting-points));
5. waits for the monitor's new incident (read through the dashboard API) and checks it against
   the table above;
6. saves the result. **The experiment stops at the first unexpected result.**

## Crash-loop limit

`demo-service.service` has `StartLimitBurst=5` within `StartLimitIntervalSec=60`: if it is
started more than 5 times in 60 s, systemd stops restarting it (`Result=start-limit-hit`).
`kill` and `crash` each cause one start; a `hang` causes none.

What the script does, and what it **cannot** promise:

- It injects a failure only after the service has been up for **at least 20 s**. That spaces
  *its own* injections at least 20 s apart, so on their own they cause at most 4 starts in any
  60 s window. `--runs` defaults to 3 and is capped at 5.
- That is **not** a guarantee that the limit is never reached. systemd does not expose its
  start-limit counter, and every start counts, including ones the script did not cause: manual
  restarts, crashes, or another tool, either shortly before the experiment (several starts
  can fit before the 20 s uptime the script checks) or while it runs.
- If the limit is hit, the script detects it (`ActiveState=failed`, `Result=start-limit-hit`),
  injects nothing further, saves the run with `"outcome": "start-limit-hit"`, exits with code
  `3`, and prints the recovery steps:

  ```bash
  sudo systemctl reset-failed demo-service.service
  sudo systemctl start demo-service.service
  ```

  It also refuses to start if the unit is already in that state. The dashboard shows the
  open incident with "systemd hit its restart limit".

## Stop

- **Ctrl+C** (or `SIGTERM`) stops the experiment at any point. No further failures are
  injected. A run that was interrupted is not saved as a result; the monitor still records its
  incident.
- **Hang cleanup.** If a hang is in progress, the script sends `SIGCONT` to every process of
  `demo-service` (`--kill-whom=all`; `SIGCONT` has no effect on processes that are not
  stopped, so a stopped process is resumed even if it is no longer the main one). It then
  checks, for up to 5 s, using the process's **identity** (PID plus its start time from
  `/proc/<pid>/stat`, so a reused PID number is recognised as a different process):

  | Observation | Result |
  |---|---|
  | same process, state `R`/`S`/`D`, `/health` answers with its PID, systemd still has it as main PID, and **every** process of the unit was read and none is stopped | **resumed** |
  | the process (or another process of the unit) is still `T` (stopped) | not resumed |
  | the process is a zombie (`Z`), dead (`X`) or gone | not resumed: it **exited** |
  | systemd's main process is now a different process (new PID, or same PID number reused) and every process of the unit was read and none is stopped | **PID changed during the hang**: run discarded |
  | anything else, e.g. an unusual state, the unit's process list (`/sys/fs/cgroup/.../cgroup.procs`) cannot be read, or the state of any process in it cannot be read | not confirmed (even if the hung process itself is healthy) |

  "None stopped" is only claimed when every process in the unit's cgroup was actually read. A
  process that was seen stopped is always reported as stopped. Because the check repeats for up
  to 5 s, a process that simply exits between being listed and being read is looked at again.

  If the command fails or times out (30 s), or the result is not "resumed" or "PID changed", it
  tries once more, then prints `WARNING: could not confirm that demo-service.service resumed`,
  what it observed, and the command to run; the exit code is `4`. Resumption is attempted and
  checked, not guaranteed. To resume it yourself, and to check:

  ```bash
  sudo systemctl kill --kill-whom=all --signal=SIGCONT demo-service.service
  curl -s http://127.0.0.1:8080/health
  ```

  The same applies if the script itself is killed with `SIGKILL` (it then cannot clean up);
  the dashboard shows an open `unhealthy-but-running` incident.
- **PID changes during a hang.** Right after `SIGSTOP`, the script checks that the process
  systemd stopped is the one it recorded (same PID and start time) and that it really is
  stopped. If the service was restarted in between, or is restarted or its process exits while
  stopped, the run no longer measures a hang of one process: it is saved with
  `"outcome": "pid-changed-during-hang"` (and what was observed), not counted as a result, and
  no further failures are injected (exit code `1`).
- The hang scenario needs `/proc/<pid>/stat` to identify the process; if it cannot be read, the
  script refuses to start the hang instead of guessing.
- **One experiment at a time on this machine**, whatever `--out-dir` is used. The lock is an
  abstract Unix socket (`@self-healing-lab-experiment`): the kernel releases it when the
  process ends, even after a crash, so there is no stale lock file.

Exit codes: `0` all runs as expected; `1` unexpected result, error, stopped by you, or main
PID changed during a hang; `3` start limit hit; `4` a hang could not be confirmed as resumed.

## Results and existing records

- Printed as a table (individual runs) and saved to
  `experiments/results/<UTC time>-<scenario>.jsonl`, one line per run: incident id, PIDs,
  detection time, recovery interval, timing basis, `client_recovery_s`, and every check.
- **Existing incident records are preserved.** The script never opens the monitor's files; it
  only reads them through the dashboard's read-only API. The monitor appends experiment
  incidents to `incidents.jsonl` like any other; the result file lists their ids so they can be
  told apart.

## Two measurements with different starting points

Each run has the monitor's recovery interval *and* the script's own `client_recovery_s`. They
start at different moments, so **do not subtract or directly compare them**:

| | Starts at | Ends at |
|---|---|---|
| monitor `recovery_interval_s` (kill, crash) | systemd's recorded exit of the old process | first healthy monitor poll (a window, ~1 s polling) |
| script `client_recovery_s`, `kill` | just **before** the script starts `sudo systemctl kill` | first OK `/health` with a new PID (0.1 s polling) |
| script `client_recovery_s`, `crash` | just before the `POST /crash` request is sent | same |
| script `client_recovery_s`, `hang` | after the `SIGCONT` command returned | first OK `/health` with the same PID |

For `kill`, `client_recovery_s` therefore includes the latency of the kill command itself
(starting `sudo` and `systemctl`, the request to systemd, signal delivery), which the monitor's
interval does not. That latency is recorded separately as `inject_command_s` (time until the
command returned). For a hang, `client_recovery_s` only measures how quickly the process
answers after resuming; it says nothing about the length of the outage.

## Safety

- The only unit it acts on is the constant `demo-service.service`; signals go through
  `systemctl kill --kill-whom=main`, so systemd picks the PID (no risk of hitting a reused PID).
  Only `SIGKILL`, `SIGSTOP` and `SIGCONT` are possible. On systemd older than 252 the script
  uses the older spelling `--kill-who`.
- Injections use `sudo -n` so they never stop at a password prompt mid-run; resuming a hang may
  prompt rather than be skipped (with a 30 s timeout).
- Hang length is limited to 3-30 s.
