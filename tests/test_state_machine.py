"""Unit tests for DHCPStateMachine.

These tests run a single-node Raft cluster (no peers) and verify the state
machine's apply and restore logic for the core DHCP operations:
grant, renew, release, expiration, reservation, and pool handling.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

from pysyncobj import SyncObjConf

from src.raft.state_machine import DHCPStateMachine

POOL_START = '10.0.0.100'
POOL_END   = '10.0.0.110'
LEASE_TIME = 300  # 5 minutes


def _make_sm(tmpdir: str, port: int = 14320) -> DHCPStateMachine:
    """Create a single-node DHCPStateMachine for testing."""
    conf = SyncObjConf(
        autoTick=True,
        fullDumpFile=os.path.join(tmpdir, 'snap.bin'),
        journalFile=os.path.join(tmpdir, 'journal.bin'),
        commandsWaitLeader=False,
        appendEntriesPeriod=0.05,   # heartbeat
        raftMinTimeout=0.2,         # > appendEntriesPeriod * 3 (= 0.15)
        raftMaxTimeout=0.5,         # > raftMinTimeout
        connectionTimeout=1.0,      # >= raftMaxTimeout
        connectionRetryTime=0.1,
    )
    sm = DHCPStateMachine(
        self_addr=f'localhost:{port}',
        partners=[],
        conf=conf,
    )
    return sm


def _wait_leader(sm: DHCPStateMachine, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            st = sm.getStatus()
            if st.get('leader') == sm.selfNode:
                return
        except Exception:
            pass
        time.sleep(0.05)
    raise TimeoutError('Single-node Raft did not elect a leader within timeout')


class TestStateMachineLeaseOps(unittest.TestCase):
    """Verify grant / renew / release / expiry logic."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = _make_sm(self.tmpdir, port=14320)
        _wait_leader(self.sm)

    def tearDown(self):
        self.sm.destroy()

    def _assign(self, mac, hostname='', req=None):
        return self.sm.try_assign_ip(
            mac, hostname, req,
            POOL_START, POOL_END, LEASE_TIME,
            sync=True, timeout=5.0,
        )

    # ── Grant ────────────────────────────────────────────────────────

    def test_grant_new_lease_returns_ip(self):
        ip = self._assign('aa:bb:cc:dd:ee:01')
        self.assertIsNotNone(ip)
        self.assertTrue(ip.startswith('10.0.0.'))

    def test_grant_stores_lease(self):
        mac = 'aa:bb:cc:dd:ee:02'
        ip = self._assign(mac, hostname='client1')
        lease = self.sm.get_lease(mac)
        self.assertIsNotNone(lease)
        self.assertEqual(lease['ip'], ip)
        self.assertEqual(lease['mac'], mac)
        self.assertEqual(lease['hostname'], 'client1')

    def test_grant_two_clients_different_ips(self):
        ip1 = self._assign('aa:bb:cc:00:00:01')
        ip2 = self._assign('aa:bb:cc:00:00:02')
        self.assertIsNotNone(ip1)
        self.assertIsNotNone(ip2)
        self.assertNotEqual(ip1, ip2)

    def test_requested_ip_honoured_when_free(self):
        ip = self._assign('aa:bb:cc:00:00:03', req='10.0.0.105')
        self.assertEqual(ip, '10.0.0.105')

    def test_requested_ip_ignored_when_taken(self):
        self._assign('aa:bb:cc:00:00:04', req='10.0.0.105')
        ip2 = self._assign('aa:bb:cc:00:00:05', req='10.0.0.105')
        # Second client should get a different IP
        self.assertNotEqual(ip2, '10.0.0.105')

    # ── Renew ────────────────────────────────────────────────────────

    def test_renew_same_ip(self):
        mac = 'aa:bb:cc:00:01:01'
        ip1 = self._assign(mac)
        ip2 = self._assign(mac)  # renew
        self.assertEqual(ip1, ip2)

    def test_renew_extends_expiry(self):
        mac = 'aa:bb:cc:00:01:02'
        self._assign(mac)
        t1 = self.sm.get_lease(mac)['expires_at']
        time.sleep(0.1)
        self._assign(mac)
        t2 = self.sm.get_lease(mac)['expires_at']
        self.assertGreaterEqual(t2, t1)

    # ── Release ──────────────────────────────────────────────────────

    def test_release_removes_lease(self):
        mac = 'aa:bb:cc:00:02:01'
        self._assign(mac)
        self.sm.release_ip(mac, sync=True, timeout=5.0)
        self.assertIsNone(self.sm.get_lease(mac))

    def test_released_ip_can_be_reassigned(self):
        mac1 = 'aa:bb:cc:00:02:02'
        ip = self._assign(mac1, req='10.0.0.108')
        self.sm.release_ip(mac1, sync=True, timeout=5.0)

        mac2 = 'aa:bb:cc:00:02:03'
        ip2 = self._assign(mac2, req='10.0.0.108')
        self.assertEqual(ip2, ip)

    # ── Expiry ───────────────────────────────────────────────────────

    def test_purge_expired_removes_leases(self):
        # Assign with a very short lease time
        mac = 'aa:bb:cc:00:03:01'
        self.sm.try_assign_ip(
            mac, '', None, POOL_START, POOL_END, 1,
            sync=True, timeout=5.0,
        )
        time.sleep(1.1)
        removed = self.sm.purge_expired(sync=True, timeout=5.0)
        self.assertGreaterEqual(removed, 1)
        self.assertIsNone(self.sm.get_lease(mac))

    def test_expired_ip_reused(self):
        mac1 = 'aa:bb:cc:00:03:02'
        # Force-write a lease dict directly to simulate expiry
        self.sm._leases[mac1] = {
            'ip': '10.0.0.109',
            'mac': mac1,
            'hostname': '',
            'assigned_at': time.time() - 10,
            'expires_at': time.time() - 5,
        }
        mac2 = 'aa:bb:cc:00:03:03'
        ip = self._assign(mac2, req='10.0.0.109')
        self.assertEqual(ip, '10.0.0.109')

    # ── Reservations ─────────────────────────────────────────────────

    def test_reservation_honoured(self):
        mac = 'aa:bb:cc:00:04:01'
        self.sm.set_reservation(mac, '10.0.0.107', sync=True, timeout=5.0)
        ip = self._assign(mac)
        self.assertEqual(ip, '10.0.0.107')

    def test_reservation_overrides_requested_ip(self):
        mac = 'aa:bb:cc:00:04:02'
        self.sm.set_reservation(mac, '10.0.0.106', sync=True, timeout=5.0)
        ip = self._assign(mac, req='10.0.0.108')
        self.assertEqual(ip, '10.0.0.106')

    def test_remove_reservation(self):
        mac = 'aa:bb:cc:00:04:03'
        self.sm.set_reservation(mac, '10.0.0.104', sync=True, timeout=5.0)
        self.sm.remove_reservation(mac, sync=True, timeout=5.0)
        res = self.sm.get_reservations()
        self.assertNotIn(mac, res)

    # ── Pool exhaustion ──────────────────────────────────────────────

    def test_pool_exhaustion_returns_none(self):
        # Pool is 10.0.0.100–110 = 11 IPs
        macs = [f'aa:00:00:00:00:{i:02x}' for i in range(12)]
        ips = [self._assign(m) for m in macs]
        # At least one assignment should fail (None) when pool is full
        none_count = sum(1 for ip in ips if ip is None)
        self.assertGreater(none_count, 0)


class TestStateMachineGetAllLeases(unittest.TestCase):
    """Verify get_all_leases reflects committed state."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = _make_sm(self.tmpdir, port=14321)
        _wait_leader(self.sm)

    def tearDown(self):
        self.sm.destroy()

    def test_get_all_leases_empty(self):
        self.assertEqual(self.sm.get_all_leases(), [])

    def test_get_all_leases_after_assign(self):
        self.sm.try_assign_ip(
            'bb:bb:bb:00:00:01', 'h1', None,
            POOL_START, POOL_END, LEASE_TIME,
            sync=True, timeout=5.0,
        )
        leases = self.sm.get_all_leases()
        self.assertEqual(len(leases), 1)
        self.assertEqual(leases[0]['hostname'], 'h1')


class TestStateMachineImport(unittest.TestCase):
    """Verify bulk import / migration helpers."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = _make_sm(self.tmpdir, port=14322)
        _wait_leader(self.sm)

    def tearDown(self):
        self.sm.destroy()

    def test_import_leases(self):
        now = time.time()
        leases = [
            {'ip': '10.0.0.100', 'mac': 'cc:00:00:00:00:01', 'hostname': 'h1',
             'assigned_at': now - 60, 'expires_at': now + 3600},
            {'ip': '10.0.0.101', 'mac': 'cc:00:00:00:00:02', 'hostname': 'h2',
             'assigned_at': now - 60, 'expires_at': now + 3600},
        ]
        count = self.sm.import_leases(leases, sync=True, timeout=5.0)
        self.assertEqual(count, 2)
        self.assertIsNotNone(self.sm.get_lease('cc:00:00:00:00:01'))
        self.assertIsNotNone(self.sm.get_lease('cc:00:00:00:00:02'))

    def test_import_skips_expired_leases(self):
        now = time.time()
        leases = [
            {'ip': '10.0.0.102', 'mac': 'dd:00:00:00:00:01', 'hostname': 'old',
             'assigned_at': now - 7200, 'expires_at': now - 3600},  # expired
        ]
        count = self.sm.import_leases(leases, sync=True, timeout=5.0)
        self.assertEqual(count, 0)
        self.assertIsNone(self.sm.get_lease('dd:00:00:00:00:01'))

    def test_import_reservations(self):
        reservations = {'ee:00:00:00:00:01': '10.0.0.103'}
        self.sm.import_reservations(reservations, sync=True, timeout=5.0)
        res = self.sm.get_reservations()
        self.assertEqual(res.get('ee:00:00:00:00:01'), '10.0.0.103')


class TestStateMachineOfferIp(unittest.TestCase):
    """Verify the non-replicated offer_ip read path."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.sm = _make_sm(self.tmpdir, port=14323)
        _wait_leader(self.sm)

    def tearDown(self):
        self.sm.destroy()

    def _pool(self):
        from src.raft.state_machine import DHCPStateMachine
        return DHCPStateMachine._build_pool(POOL_START, POOL_END)

    def test_offer_ip_empty_pool(self):
        pool = self._pool()
        ip = self.sm.offer_ip('ff:00:00:00:00:01', None, pool)
        self.assertEqual(ip, '10.0.0.100')  # first free

    def test_offer_ip_existing_lease(self):
        pool = self._pool()
        mac = 'ff:00:00:00:00:02'
        self.sm.try_assign_ip(
            mac, '', '10.0.0.105', POOL_START, POOL_END, LEASE_TIME,
            sync=True, timeout=5.0,
        )
        ip = self.sm.offer_ip(mac, None, pool)
        self.assertEqual(ip, '10.0.0.105')

    def test_offer_ip_reservation_wins(self):
        pool = self._pool()
        mac = 'ff:00:00:00:00:03'
        self.sm.set_reservation(mac, '10.0.0.109', sync=True, timeout=5.0)
        ip = self.sm.offer_ip(mac, '10.0.0.100', pool)
        self.assertEqual(ip, '10.0.0.109')


if __name__ == '__main__':
    unittest.main()
