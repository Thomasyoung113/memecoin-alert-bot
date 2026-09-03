"""
Web dashboard for Alert Bot — lightweight HTTP server using only stdlib.
Serves a live-updating HTML dashboard and JSON API endpoints backed by bot.models.
"""
import hmac
import json
import logging
import os
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from bot.models import execute, close_cursor, _scalar, _dict_rows
from config import DASHBOARD_TOKEN

logger = logging.getLogger("dashboard")

HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"

# How far back in the log file to read (bytes)
LOG_TAIL_BYTES = 16 * 1024  # 16 KB
LOG_PATH = HERE.parent / "bot.log"

PORT = int(os.getenv("PORT", "8080"))

# Allowed origins for CORS — only send Access-Control-Allow-Origin if Origin matches
ALLOWED_ORIGINS = {
    "http://localhost:8080",
    "http://127.0.0.1:8080",
    "http://localhost",
    "http://127.0.0.1",
}


class _DashboardHandler(SimpleHTTPRequestHandler):
    """Request handler that serves the dashboard HTML + JSON API endpoints."""

    # Silence default request logging (we keep our own)
    def log_message(self, format, *args):
        logger.debug("HTTP %s — %s", self.address_string(), format % args)

    def _cors_origin(self) -> str | None:
        """Return the origin if it's in the allowed list, else None."""
        origin = self.headers.get("Origin")
        if origin and origin in ALLOWED_ORIGINS:
            return origin
        return None

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        origin = self._cors_origin()
        if origin is not None:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, _msg: str, status=500):
        self._send_json({"error": "internal error"}, status)

    def _route_api_stats(self):
        """Return total / success / pending counts."""
        try:
            c = execute("SELECT COUNT(*) FROM alerts")
            total_alerts = _scalar(c) or 0
            close_cursor(c)
            c = execute("SELECT COUNT(*) FROM alerts WHERE resolved = 1 AND hit_2x = 1")
            total_success = _scalar(c) or 0
            close_cursor(c)
            c = execute("SELECT COUNT(*) FROM alerts WHERE resolved = 0")
            pending = _scalar(c) or 0
            close_cursor(c)
            c = execute("SELECT COUNT(*) FROM alerts WHERE resolved = 1")
            resolved = _scalar(c) or 0
            close_cursor(c)
            success_rate = (total_success / resolved * 100) if resolved > 0 else 0.0
            self._send_json({
                "total_alerts": total_alerts,
                "total_success": total_success,
                "resolved": resolved,
                "pending": pending,
                "success_rate": round(success_rate, 1),
            })
        except Exception as e:
            logger.exception("Error fetching stats")
            self._send_error_json(str(e))

    def _route_api_alerts(self):
        """Return recent alerts (latest 50), newest first."""
        try:
            c = execute("""
                SELECT id, token_address, symbol, alert_mcap, alert_price,
                       target_2x_mcap, hit_2x, resolved, alert_time, peak_mcap
                FROM alerts
                ORDER BY id DESC
                LIMIT 50
            """)
            rows = _dict_rows(c)
            close_cursor(c)
            self._send_json(rows)
        except Exception as e:
            logger.exception("Error fetching alerts")
            self._send_error_json(str(e))

    def _route_api_logs(self):
        """Return the last N lines of bot.log."""
        try:
            if not LOG_PATH.exists():
                self._send_json({"lines": [], "truncated": False})
                return
            size = LOG_PATH.stat().st_size
            read_size = min(LOG_TAIL_BYTES, size)
            with open(LOG_PATH, "rb") as f:
                if read_size < size:
                    f.seek(-read_size, os.SEEK_END)
                    # Skip the first (possibly partial) line
                    f.readline()
                raw = f.read().decode("utf-8", errors="replace")
            lines = [line.rstrip("\n\r") for line in raw.splitlines()]
            self._send_json({"lines": lines, "truncated": read_size < size})
        except Exception as e:
            logger.exception("Error reading log")
            self._send_error_json(str(e))

    def _check_auth(self) -> bool:
        """Return True if the request is authenticated (or auth is disabled)."""
        if not DASHBOARD_TOKEN:
            return True
        # Check query parameter first, then header
        from urllib.parse import parse_qs, urlparse
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        token = (params.get("token") or [None])[0]
        if hmac.compare_digest(token or '', DASHBOARD_TOKEN):
            return True
        header_token = self.headers.get("X-Auth-Token")
        if hmac.compare_digest(header_token or '', DASHBOARD_TOKEN):
            return True
        return False

    def do_GET(self):
        path = self.path.split("?")[0]  # strip query params
        if path.startswith("/api/") and not self._check_auth():
            self._send_json({"error": "unauthorized"}, 401)
            return
        if path == "/":
            self._serve_index()
        elif path == "/api/stats":
            self._route_api_stats()
        elif path == "/api/alerts":
            self._route_api_alerts()
        elif path == "/api/logs":
            self._route_api_logs()
        else:
            self._send_json({"error": "Not found"}, 404)

    def _serve_index(self):
        index_path = TEMPLATES / "index.html"
        if not index_path.exists():
            self._send_error_json("index.html not found", 500)
            return
        html = index_path.read_text(encoding="utf-8")
        # Inject the dashboard token so the frontend JS can use it for API calls
        if DASHBOARD_TOKEN:
            # Insert a meta tag with the token right before the closing </head>
            meta = f'<meta name="dashboard-token" content="{DASHBOARD_TOKEN}">\n'
            html = html.replace("</head>", f"{meta}</head>")
        self._send_html(html)


class DashboardServer:
    """Thin wrapper around HTTPServer that runs in a daemon thread."""

    def __init__(self, host: str = "0.0.0.0", port: int = PORT):
        self.host = host
        self.port = port
        self._server = ThreadingHTTPServer((host, port), _DashboardHandler)
        self._thread: threading.Thread | None = None

    def start(self):
        """Start the server in a daemon thread."""
        if self._thread and self._thread.is_alive():
            logger.warning("Dashboard already running on %s:%d", self.host, self.port)
            return

        def _serve():
            logger.info(
                "Dashboard starting on http://%s:%d", self.host, self.port
            )
            self._server.serve_forever()

        self._thread = threading.Thread(target=_serve, daemon=True, name="dashboard")
        self._thread.start()
        logger.info("Dashboard thread started")

    def stop(self):
        """Shutdown the HTTP server."""
        self._server.shutdown()
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("Dashboard stopped")


# Convenience singleton
_default_server: DashboardServer | None = None


def start_dashboard(host: str = "0.0.0.0", port: int = PORT):
    """Start the dashboard server in a daemon thread (idempotent)."""
    global _default_server
    if _default_server is not None:
        return _default_server
    _default_server = DashboardServer(host, port)
    _default_server.start()
    return _default_server