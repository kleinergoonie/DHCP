"""Headless DHCP server entry point for multi-node cluster deployments.

This module provides a command-line interface for running the DHCP server
without the tkinter GUI.  It is designed for use in server environments,
containers, and systemd services.

Usage examples
--------------
Bootstrap a brand-new 3-node cluster (run on each node with its own config)::

    python raft_server.py --config node1.json

Join an existing cluster::

    python raft_server.py --config node2.json

Single-node mode (backward compatible)::

    python raft_server.py --config config.json

Show cluster status::

    python raft_server.py --config node1.json --status

Migrate legacy leases from a JSON dump::

    python raft_server.py --config node1.json --import-leases leases.json

Export current in-memory leases to JSON (for migration)::

    python raft_server.py --config config.json --export-leases leases.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time

# Ensure the project root is on sys.path when invoked directly
sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(name)s  %(message)s',
)
logger = logging.getLogger('raft_server')


def _load_config(path: str) -> dict:
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def _show_status(server) -> None:
    lm = server.lease_manager
    if hasattr(lm, 'get_cluster_status'):
        st = lm.get_cluster_status()
        print(json.dumps(st, indent=2))
    else:
        print('Raft is not enabled in this configuration.')
        print(f'Active leases: {len(lm.get_all_leases())}')


def _export_leases(source_lm, path: str) -> None:
    from src.raft.migration import dump_leases_to_json
    dump_leases_to_json(source_lm, path)
    print(f'Leases exported to {path}')


def _import_leases(path: str, server) -> None:
    lm = server.lease_manager
    if not hasattr(lm, 'get_cluster_status'):
        print('ERROR: Raft is not enabled – cannot import into Raft cluster.')
        sys.exit(1)
    from src.raft.migration import migrate_from_json
    print(f'Waiting for Raft cluster to become ready…')
    n_leases, n_res = migrate_from_json(path, lm, timeout=60.0)
    print(f'Imported {n_leases} leases and {n_res} reservations.')


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description='Headless DHCP server with optional Raft failover',
    )
    parser.add_argument(
        '--config', default='config.json',
        help='Path to config.json (default: config.json)',
    )
    parser.add_argument(
        '--status', action='store_true',
        help='Print cluster status and exit',
    )
    parser.add_argument(
        '--export-leases', metavar='FILE',
        help='Export current leases to a JSON file and exit',
    )
    parser.add_argument(
        '--import-leases', metavar='FILE',
        help='Import leases from a JSON file into the Raft cluster and exit',
    )
    parser.add_argument(
        '--log-level', default='INFO',
        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
        help='Logging verbosity',
    )
    args = parser.parse_args(argv)

    logging.getLogger().setLevel(args.log_level)

    config = _load_config(args.config)

    from src.dhcp_server import DHCPServer

    def on_log(msg: str) -> None:
        logger.info('%s', msg)

    server = DHCPServer(config=config, on_log=on_log)

    # ── One-shot operations ──────────────────────────────────────────
    if args.export_leases:
        _export_leases(server.lease_manager, args.export_leases)
        return

    if args.status:
        # Need to start briefly to connect Raft before querying status
        if config.get('raft_enabled'):
            logger.info('Connecting to Raft cluster (max 10s)…')
            server.start()
            time.sleep(10)
            _show_status(server)
            server.stop()
        else:
            _show_status(server)
        return

    # ── Start server ─────────────────────────────────────────────────
    server.start()

    if config.get('raft_enabled'):
        lm = server.lease_manager
        logger.info('Raft enabled.  Waiting for cluster to be ready…')
        ready = lm.wait_ready(timeout=60.0)
        if ready:
            logger.info(
                'Cluster ready.  Leader: %s  This node is leader: %s',
                lm.get_cluster_status().get('leader', '?'),
                lm.is_leader,
            )
        else:
            logger.warning(
                'Cluster not ready after 60s – continuing anyway '
                '(may operate in degraded mode until leader elected).'
            )

        # Optionally import leases after cluster is ready
        if args.import_leases:
            _import_leases(args.import_leases, server)

        # Start metrics server
        metrics_port = config.get('raft_metrics_port', 9090)
        if metrics_port:
            from src.raft.metrics import MetricsServer
            metrics = MetricsServer(lm, port=metrics_port)
            metrics.start()
            logger.info('Metrics available at http://localhost:%d/metrics', metrics_port)
            logger.info('Health   available at http://localhost:%d/health', metrics_port)

    # ── Graceful shutdown ────────────────────────────────────────────
    stop_event = __import__('threading').Event()

    def _on_signal(signum, frame):
        logger.info('Signal %d received – shutting down…', signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT,  _on_signal)

    logger.info('DHCP server running.  Press Ctrl+C to stop.')
    stop_event.wait()
    server.stop()
    logger.info('Server stopped.')


if __name__ == '__main__':
    main()
