"""Prometheus-compatible HTTP metrics and health endpoint.

Exposes two paths:

* ``GET /metrics`` – Prometheus text exposition format with basic Raft gauges.
* ``GET /health``  – JSON body indicating node role and readiness.

Usage::

    server = MetricsServer(raft_lease_manager, port=9090)
    server.start()   # background thread
    ...
    server.stop()
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .lease_manager import RaftLeaseManager

logger = logging.getLogger(__name__)


class _Handler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for /metrics and /health."""

    raft_lm: 'RaftLeaseManager'  # injected by MetricsServer

    def log_message(self, *args):
        pass  # suppress default access log

    def do_GET(self):
        if self.path == '/metrics':
            self._serve_metrics()
        elif self.path == '/health':
            self._serve_health()
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'Not found\n')

    def _serve_metrics(self):
        try:
            st = self.raft_lm.get_cluster_status()
        except Exception as exc:
            logger.error('Metrics error: %s', exc)
            self.send_response(500)
            self.end_headers()
            return

        lines = [
            '# HELP dhcp_raft_is_leader 1 if this node is the Raft leader',
            '# TYPE dhcp_raft_is_leader gauge',
            f'dhcp_raft_is_leader {1 if st["is_leader"] else 0}',
            '',
            '# HELP dhcp_raft_is_ready 1 if this node is synced with the cluster',
            '# TYPE dhcp_raft_is_ready gauge',
            f'dhcp_raft_is_ready {1 if st["is_ready"] else 0}',
            '',
            '# HELP dhcp_raft_term Current Raft election term',
            '# TYPE dhcp_raft_term gauge',
            f'dhcp_raft_term {st["raft_term"]}',
            '',
            '# HELP dhcp_raft_commit_index Index of the last committed log entry',
            '# TYPE dhcp_raft_commit_index gauge',
            f'dhcp_raft_commit_index {st["commit_index"]}',
            '',
            '# HELP dhcp_raft_last_applied Index of the last applied log entry',
            '# TYPE dhcp_raft_last_applied gauge',
            f'dhcp_raft_last_applied {st["last_applied"]}',
            '',
            '# HELP dhcp_raft_has_quorum 1 if cluster has quorum',
            '# TYPE dhcp_raft_has_quorum gauge',
            f'dhcp_raft_has_quorum {1 if st["has_quorum"] else 0}',
            '',
            '# HELP dhcp_raft_partner_count Number of configured partner nodes',
            '# TYPE dhcp_raft_partner_count gauge',
            f'dhcp_raft_partner_count {st["partner_count"]}',
            '',
            '# HELP dhcp_active_leases Number of active (non-expired) leases',
            '# TYPE dhcp_active_leases gauge',
            f'dhcp_active_leases {st["lease_count"]}',
            '',
        ]

        body = '\n'.join(lines).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_health(self):
        try:
            st = self.raft_lm.get_cluster_status()
        except Exception as exc:
            logger.error('Health error: %s', exc)
            self.send_response(500)
            self.end_headers()
            return

        role = 'leader' if st['is_leader'] else 'follower'
        accepting = st['is_leader'] and st['is_ready']

        body = json.dumps({
            'node': st['node'],
            'role': role,
            'is_ready': st['is_ready'],
            'accepting_allocations': accepting,
            'leader': st['leader'],
            'raft_term': st['raft_term'],
            'commit_index': st['commit_index'],
            'last_applied': st['last_applied'],
        }).encode()

        status_code = 200 if st['is_ready'] else 503
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MetricsServer:
    """Tiny HTTP server exposing Raft metrics and health endpoints.

    Parameters
    ----------
    raft_lm:
        Running :class:`~src.raft.lease_manager.RaftLeaseManager`.
    port:
        TCP port to listen on (default 9090).  Pass 0 to bind to any free
        port (useful in tests).
    """

    def __init__(self, raft_lm: 'RaftLeaseManager', port: int = 9090) -> None:
        self._lm = raft_lm
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The actual bound port (useful when port=0 was requested)."""
        if self._server:
            return self._server.server_address[1]
        return self._port

    def start(self) -> None:
        """Start the metrics server in a daemon background thread."""
        handler_cls = type('_H', (_Handler,), {'raft_lm': self._lm})
        self._server = HTTPServer(('', self._port), handler_cls)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        logger.info('Metrics server listening on port %d', self.port)

    def stop(self) -> None:
        """Stop the metrics server."""
        if self._server:
            self._server.shutdown()
            self._server = None
