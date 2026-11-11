# Monitor: detection and measurement

`demo-monitor.service` polls (target: once per second):

| Signal | Source |
|---|---|
| State, PID, restart count, exit cause | `systemctl show demo-service` (ActiveState, SubState, MainPID, NRestarts, ExecMainCode/Status) |
| Memory | `MemoryCurrent` (cgroup, bytes) |
| CPU % | change in `CPUUsageNSec` (cgroup CPU time) / time between polls; 100% = one core |
| Health | `GET /health`; the PID it reports must equal systemd's `MainPID` |
| Logs | the demo's own log file `/var/log/self-healing-lab/demo.log` |

Output in `/var/lib/self-healing-lab/` (directory 2750 `shlab-mon:shlab-dash`, files 0640;
see [DASHBOARD.md](DASHBOARD.md#how-it-reads-the-protected-monitor-files) for why the dashboard's group can read them):

- `status.json` - current status, last 300 polls (incl. how long each systemctl / health
  call took and the gap between polls), planned restarts, recent log lines
- `incidents.jsonl` - one JSON line per closed incident, with timings and a log snapshot

```bash
journalctl -u demo-monitor -f                 # terminal 1: live events
sudo systemctl kill -s SIGKILL demo-service   # terminal 2
sudo tail -n 1 /var/lib/self-healing-lab/incidents.jsonl | python3 -m json.tool
```

## Classification

| What the monitor sees | Result |
|---|---|
| systemd: `auto-restart`, `failed`, or `inactive` with non-success `Result` | incident, `classification: failure` |
| `active`, but `/health` fails (or reports a different PID) on 2 consecutive polls | incident `unhealthy-but-running`, `failure` |
| one single failed health check | status `degraded`, no incident |
| `activating`/`deactivating` for longer than 15 s | incident `stuck-transition` |
| clean stop (`inactive`, `Result=success`) then start | **planned restart**, not an incident (listed in `status.json`) |
| PID changed between two healthy polls, `NRestarts` increased | incident `failure`, `missed_down_window: true` |
| PID changed, `NRestarts` did *not* increase, no clean stop seen | record with `classification: uncertain`: most likely a manual `systemctl restart`, but the monitor cannot confirm who triggered it |
| `systemctl` fails or times out | status `monitor-error`; no incident is opened **or closed** on that poll |

`NRestarts` counts restarts done by systemd's `Restart=` logic, which is why an increase is
used as evidence of an automatic (failure-triggered) restart. Each record also states
`recovered_by` (automatic restart, same process recovered, or restart not counted as automatic,
e.g. after a manual `reset-failed` + `start`).

**PID cross-check:** systemd and `/health` are queried one after the other. If the PIDs differ,
the monitor asks systemd again (the process may have been replaced in between). If they still
differ, the health check counts as failed. This prevents another process listening on port
8080 from "closing" an incident.

## How measurement works

```
  process exit      monitor poll       systemd starts      first healthy
  recorded by       sees failure       new process         poll completes
  systemd
  (failed_at)       (detected_at)      (restarted_at)      (healthy_at)
       |-----------------|-------------------|-------------------|
       |<-time_to_detect>|                   |                   |
       |<----------- restart_delay --------->|                   |
       |<------------------------ recovery_time_s -------------->|
```

Each incident records its `timing_basis`: `systemd-exit-timestamp` (process exit seen),
`first-unhealthy-poll` (hang: no exit to timestamp) or `unknown` (exit not captured).

- **failed_at**: systemd's `ExecMainExitTimestampMonotonic`. This is when systemd *recorded*
  the exit while handling it, with microsecond resolution; it is not guaranteed to be the
  exact instant the process died. systemd clears it when the next process starts, so it is
  only captured if a poll lands in the restart window (`RestartSec=2` makes that likely but
  not guaranteed). If it was not captured, `recovery_time_s` is left empty and only
  `recovery_upper_bound_from_last_ok_s` is reported: from the start of the last poll that saw
  the service healthy to the end of the first poll that saw it healthy again.
- **restarted_at**: systemd's `ExecMainStartTimestampMonotonic` of the new process.
- **healthy_at**: the end of the first poll that saw the service healthy.
- **Hangs are measured from detection, not from when the problem started.** A hung process
  does not exit, so there is no systemd timestamp. `failed_at` is then the start of the
  *first poll that saw the service unhealthy* (`timing_basis: first-unhealthy-poll`). The hang
  itself may have started earlier, so for a hang:
  - the recovery interval describes the time from first detection to recovery; it is **not a
    bound on the full outage**, which may have been longer;
  - `time_to_detect_s` is left empty (there is no known start to measure from);
  - `undetected_window_before_estimate_s` is the gap between the last healthy poll and the
    first unhealthy one. The hang most likely began in that gap, but even that is not
    guaranteed: a check can still pass while a service is already degrading.

All values use `CLOCK_MONOTONIC`, the clock systemd uses for its `*Monotonic` properties, so
they can be subtracted directly and are not affected by wall-clock changes.

**Uncertainty is measured per incident, not assumed.** The monitor only knows that the
service became healthy somewhere in a window, and records both ends, measured from
`failed_at`. For a crash, `failed_at` is systemd's recorded exit, so recovery happened within
the window. For a hang, `failed_at` is the first unhealthy poll, so the window can leave out
the earlier part of the outage (see the hang note above):

- `recovery_time_lower_bound_s`: from `failed_at` to the later of (start of the last poll
  that saw it unhealthy, start of the new process)
- `recovery_time_s`: from `failed_at` to the end of the first healthy poll (conservative)

The width of that window depends on actual poll timing, which can be stretched by the time
`systemctl` and the health check take, by CPU scheduling and by system load. `max_poll_gap_s`
in each incident and `poll_gap_s` / `systemctl_ms` / `health_ms` in `status.json` show what
actually happened. If the laptop suspends during an incident, the timings are not meaningful.

**Known gap:** systemd does not restart a hung-but-alive process. The monitor detects it, but
nothing heals it yet (planned improvement: `WatchdogSec=`).
