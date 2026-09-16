"""Placeholder HTTP server for service containers during Phase 0 bootstrap.

Provides /healthz, /readyz, and /metrics endpoints so container health checks
and Prometheus scraping function properly before full worker logic is deployed.
"""

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP handler responding to container health and metric checks."""

    def do_GET(self) -> None:
        service_name = os.getenv("SERVICE_NAME", "service")
        if self.path in ("/healthz", "/readyz", "/"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            payload = {"status": "healthy", "service": service_name}
            self.wfile.write(json.dumps(payload).encode("utf-8"))
        elif self.path == "/metrics":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            metrics = (
                f"# HELP up Status of {service_name}\n"
                f"# TYPE up gauge\n"
                f'up{{service="{service_name}"}} 1\n'
            )
            self.wfile.write(metrics.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Suppress standard HTTP request logging for quiet health polling."""
        pass


def run() -> None:
    """Run the placeholder HTTP health server."""
    port = int(os.getenv("PORT", "8000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    service = os.getenv("SERVICE_NAME", "service")
    print(f"[INFO] Started placeholder health server for {service} on port {port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
