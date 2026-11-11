# Dashboard

`demo-dashboard.service` serves a read-only page at **http://127.0.0.1:8081/** (open it in a
browser on the same machine). It shows:

- current status (icon + label; never colour alone), PID, automatic-restart count, CPU,
  memory and health-check latency;
- CPU and memory charts over the last ~5 minutes (hover or use the arrow keys for values; the
  same data is available as a table);
- every incident with its classification, PID change, detection time and recovery
  interval, worded according to its `timing_basis` (a hang says "measured from the first
  failed check, not from the start of the problem");
- planned restarts and the most recent demo log lines.

It refreshes every 5 seconds. If `status.json` has not been updated for more than 5 seconds,
the page shows **"Monitor data is stale"** instead of the last status, so a stopped monitor
can never make the service look healthy.

Before installing, check that port 8081 is free (no output means free):

```bash
ss -ltn 'sport = :8081'
```

## How it reads the protected monitor files

The monitor's results are not world-readable. Access is granted by Unix groups:

```
/var/lib/self-healing-lab/    drwxr-s---  shlab-mon  shlab-dash
  status.json                 -rw-r-----  shlab-mon  shlab-dash
  incidents.jsonl             -rw-r-----  shlab-mon  shlab-dash
```

| Who | Runs as | Access | Why |
|---|---|---|---|
| monitor | user `shlab-mon` | read + write | owner |
| dashboard | user `shlab-dash`, group `shlab-dash` | read only | group `r` bit; group `w` is not set |
| demo service | `shlab-demo` | none | not owner, not in the group |
| your login user | e.g. you | none (use `sudo` to inspect) | "other" has no bits |

- The **setgid bit** (`s` in `drwxr-s---`) makes every new file in the directory get group
  `shlab-dash` automatically, and the monitor creates files with mode 0640 (`UMask=0027`, and
  set explicitly in code).
- The dashboard additionally runs with `ProtectSystem=strict` and **no** writable paths, so
  the whole filesystem is read-only for it even if a permission were wrong.
- The dashboard has its **own account** instead of running as `shlab-mon`: processes of the
  same user can signal each other, so a bug in the dashboard could otherwise stop the monitor.
- The monitor unit uses `ReadWritePaths=` rather than `StateDirectory=`: systemd resets a
  `StateDirectory`'s owner and group on every start, which would remove the dashboard's access.
- Upgrading an install from before the dashboard existed: results written then were 0600 / group `shlab-mon`. The
  installer changes only the owner, group and mode of `status.json` and `incidents.jsonl`
  (never their contents, never through a symlink) so the dashboard can read them.

## Network exposure

- The server refuses to start on anything but a loopback IP (`127.0.0.1` or `::1`).
- Requests must carry a loopback `Host` header (`127.0.0.1:8081`, `localhost:8081`,
  `[::1]:8081`); others get `403`. This blocks DNS-rebinding, where a malicious web page uses
  your browser to reach local services through a name it controls.
- Only `GET`/`HEAD`; other methods get `405`. There is no button that can crash or restart
  anything.
- Strict `Content-Security-Policy` (no inline or external scripts/styles), `nosniff`, no
  framing. Log lines and causes are HTML-escaped; the page script uses `textContent` only.
- The unit adds kernel-level filtering: `IPAddressDeny=any` + `IPAddressAllow=localhost`.
  This relies on cgroup/BPF support in the kernel; check `journalctl -u demo-dashboard` for
  warnings if in doubt.

Checks after installing:

```bash
ss -ltn 'sport = :8081'                                           # expect 127.0.0.1:8081 only
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8081/                          # 200
curl -s -o /dev/null -w '%{http_code}\n' -H 'Host: evil.example' http://127.0.0.1:8081/  # 403
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://127.0.0.1:8081/                  # 405
sudo -u shlab-demo cat /var/lib/self-healing-lab/status.json      # expect: Permission denied
```

For development you can run it against any data directory:
`python3 dashboard/dashboard.py --data-dir ./data --port 8081`.
