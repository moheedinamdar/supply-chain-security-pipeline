"""Minimal sample workload.

Its only purpose is to be built, SBOM'd, scanned, signed, attested, and then
admitted (or rejected) by the Kyverno verification policy. Kept dependency-free
on purpose so the supply-chain tooling is the star, not the app.
"""

from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"supply-chain-security-pipeline: signed & verified\n")

    def log_message(self, *args) -> None:  # silence default access logging
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
