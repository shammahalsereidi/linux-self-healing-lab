#!/usr/bin/env python3
"""Demo service for the Linux Self-Healing Lab.

A tiny HTTP service whose only job is to be monitored, crashed and
restarted. It uses only the Python standard library so there is nothing
to install.

Endpoints:
    GET  /        short description
    GET  /health  JSON health info (status, pid, uptime)
    POST /crash   exits immediately with code 1 (simulated fatal error)

Logs go to stdout. Under systemd, stdout is captured by journald, so
`journalctl -u demo-service` shows them with timestamps.

If DEMO_LOG_FILE is set, the same lines are also written to that file with
an ISO-8601 timestamp. The monitor reads this file instead of the system
journal, so it does not need journal-wide read access.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("DEMO_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEMO_PORT", "8080"))
START_TIME = time.time()

LOG_FILE = os.environ.get("DEMO_LOG_FILE")


class IsoFormatter(logging.Formatter):
    """Local time with UTC offset and milliseconds, e.g. 2026-11-11T15:46:33.300+00:00"""

    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created).astimezone().isoformat(
            timespec="milliseconds")


def setup_logging():
    # stdout -> journald. No timestamp: journald records one for every line.
    handlers = [logging.StreamHandler(sys.stdout)]
    handlers[0].setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    if LOG_FILE:
        # Small, rotated file so the lab can't fill the disk.
        fh = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=2)
        fh.setFormatter(IsoFormatter("%(asctime)s %(levelname)s %(message)s"))
        handlers.append(fh)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


setup_logging()
log = logging.getLogger("demo-service")


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, content_type="application/json"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._send(200, json.dumps({
                "status": "ok",
                "pid": os.getpid(),
                "uptime_seconds": round(time.time() - START_TIME, 1),
            }))
        elif self.path == "/":
            self._send(200, "Linux Self-Healing Lab demo service. "
                            "Try GET /health or POST /crash\n", "text/plain")
        else:
            self._send(404, json.dumps({"error": "not found"}))

    def do_POST(self):
        if self.path == "/crash":
            # POST (not GET) so a browser prefetch or link preview can't
            # crash the service by accident.
            log.error("CRASH requested via /crash - exiting with code 1")
            self._send(200, json.dumps({"crashing": True, "pid": os.getpid()}))
            self.wfile.flush()
            # os._exit skips all cleanup, like a real fatal crash would.
            os._exit(1)
        self._send(404, json.dumps({"error": "not found"}))

    def log_message(self, fmt, *args):
        # Route the default per-request access log through our logger.
        log.info("%s %s", self.address_string(), fmt % args)


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    def handle_sigterm(signum, _frame):
        # `systemctl stop` sends SIGTERM. We shut down cleanly and exit 0,
        # so systemd treats it as an intended stop and does NOT restart us.
        log.info("received signal %d, shutting down cleanly", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, handle_sigterm)

    log.info("demo-service started pid=%d listening on %s:%d",
             os.getpid(), HOST, PORT)
    server.serve_forever()
    server.server_close()
    log.info("demo-service stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
