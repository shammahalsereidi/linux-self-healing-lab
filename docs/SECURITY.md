# Security design

This lab runs on a personal machine, so both services are restricted:

- The demo listens on `127.0.0.1` only (not reachable from the network).
- Each service has its own unprivileged system account with no login shell.
- `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`, `NoNewPrivileges=yes`. The only
  writable paths are the demo's log directory and the monitor's state directory.
- `MemoryMax=128M`, `CPUQuota=50%` on the demo: cgroup limits contain runaway experiments.
- **No system-journal access for the monitor.** An earlier draft added the monitor to the
  `systemd-journal` group. That group can read the *entire* system journal: every service,
  the kernel, and authentication logs (sudo usage, SSH logins), which may contain sensitive
  data. Journal file permissions are not per unit, so the group cannot be limited to one
  service. Instead, the demo writes a copy of its log to `/var/log/self-healing-lab/demo.log`
  (mode 0640, group `shlab-mon`), and the monitor reads only that. The full log, including
  systemd's own messages about the unit, is still in the journal for administrators:
  `journalctl -u demo-service`.
  (Alternative considered: `LogNamespace=` gives a unit its own journal, but systemd's
  messages *about* the unit still go to the main journal, and granting access needs ACLs on
  journal files: more moving parts for little gain here.)
- Monitor output is readable only by the monitor and the dashboard (directory 2750
  `shlab-mon:shlab-dash`, files 0640, `UMask=0027`); the demo service and normal users
  cannot read it. The dashboard cannot modify it (see [DASHBOARD.md](DASHBOARD.md)).
- The dashboard listens on `127.0.0.1` only, accepts only loopback `Host` headers, has no
  endpoints that change anything, and its unit drops non-loopback traffic (`IPAddressDeny=`).
