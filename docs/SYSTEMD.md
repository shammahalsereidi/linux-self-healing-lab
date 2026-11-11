# Self-healing with systemd

The "self-healing" in this lab is systemd's own restart logic, configured in
[`systemd/demo-service.service`](../systemd/demo-service.service):

```ini
[Unit]
StartLimitIntervalSec=60
StartLimitBurst=5          # more than 5 starts in 60 s -> stop restarting, unit "failed"

[Service]
Restart=on-failure         # restart after a non-zero exit, an unclean signal, or a timeout
RestartSec=2               # wait 2 s before restarting
```

`Restart=on-failure` (rather than `always`) is deliberate: a clean `systemctl stop` exits with
code 0 and is **not** restarted, so planned maintenance and failures can be told apart.

## What systemd does and does not restart

| Action | Process exit | Restarted by systemd? | Why | Seen on the test laptop? |
|---|---|---|---|---|
| `curl -X POST 127.0.0.1:8080/crash` | code 1 | yes | non-zero exit = failure | yes (runs 2, 7) |
| `systemctl kill -s SIGKILL demo-service` | signal 9 | yes | killed by an unclean signal | yes (runs 1, 5, 6) |
| `sudo systemctl stop demo-service` | code 0 (SIGTERM handled) | no | intended stop, not a failure | yes (run 4) |
| `SIGSTOP` (process frozen) | none: process still alive | **no** | `Restart=` only reacts to an exit | yes (run 8: resumed by the experiment script) |
| more than 5 starts within 60 s | - | no, unit enters `failed` (`Result=start-limit-hit`) | crash-loop protection | not triggered; expected from the unit settings |

Run numbers refer to [Verified results](../README.md#verified-results).

A frozen or deadlocked process is the gap in this setup: systemd sees a running process and
does nothing. The monitor detects it (`unhealthy-but-running`), but healing it would need, for
example, `WatchdogSec=` together with watchdog notifications from the app. That is not
implemented.

To recover from the crash-loop state:

```bash
sudo systemctl reset-failed demo-service
sudo systemctl start demo-service
```

## Check it by hand

```bash
# 1. Note the current PID and restart counter
systemctl show demo-service -p MainPID -p NRestarts

# 2. Kill it hard (simulates the kernel/OOM killer or a segfault)
sudo systemctl kill -s SIGKILL demo-service

# 3. Watch it come back (RestartSec=2)
sleep 3
systemctl show demo-service -p MainPID -p NRestarts
curl http://127.0.0.1:8080/health

# 4. See what happened in the logs
journalctl -u demo-service -n 20 --no-pager
```

Expected: a new `MainPID`, `NRestarts` increased by 1, and `/health` answers again.
