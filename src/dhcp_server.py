"""Core DHCP server – listens on UDP port 67 and handles client messages.

When Raft mode is enabled (``raft_enabled: true`` in config), the server
uses a :class:`~src.raft.lease_manager.RaftLeaseManager` instead of the
default in-memory :class:`~src.lease_manager.LeaseManager`.  In that mode:

* Only the **Raft leader** responds to DHCPDISCOVER and DHCPREQUEST for new
  allocations.  Follower nodes silently drop DISCOVER and NAK new REQUESTs.
* When ``raft_allow_follower_renewals`` is ``true``, followers may also
  serve DHCPREQUEST renewals for leases that already exist in their local
  state.  This improves renewal availability at the cost of potentially
  serving a slightly stale lease expiry time during leader-less intervals.
* DHCPRELEASE and DHCPINFORM are always handled locally (INFORM never
  modifies state; RELEASE is forwarded to the Raft log by any node).

Single-node / legacy mode (``raft_enabled: false``, the default) is
completely unchanged and requires no external dependencies.
"""

import ipaddress
import logging
import socket
import threading
import time
from typing import Callable

from .dhcp_packet import (
    DHCPACK, DHCPDECLINE, DHCPDISCOVER, DHCPINFORM,
    DHCPNAK, DHCPOFFER, DHCPRELEASE, DHCPREQUEST,
    MSG_TYPE_NAMES,
    OPT_DNS, OPT_LEASE_TIME, OPT_MSG_TYPE, OPT_REBINDING_TIME,
    OPT_RENEWAL_TIME, OPT_REQUESTED_IP, OPT_ROUTER, OPT_SERVER_ID,
    OPT_SUBNET_MASK,
    DHCPPacket,
)
from .lease_manager import LeaseManager

SERVER_PORT = 67
CLIENT_PORT = 68

logger = logging.getLogger(__name__)


def _build_lease_manager(
    config: dict,
    on_leases_changed: Callable | None,
):
    """Return the appropriate lease manager based on config.

    If ``raft_enabled`` is truthy in *config*, a
    :class:`~src.raft.lease_manager.RaftLeaseManager` is returned.
    Otherwise the standard in-memory :class:`~src.lease_manager.LeaseManager`
    is returned (backward-compatible default).
    """
    if config.get('raft_enabled'):
        from .raft.config import RaftConfig
        from .raft.lease_manager import RaftLeaseManager

        raft_cfg = RaftConfig.from_dict(config)
        logger.info(
            'Raft mode enabled – node=%s  bind=%s  peers=%s',
            raft_cfg.node_id or '(auto)',
            raft_cfg.bind_address,
            raft_cfg.peers,
        )
        return RaftLeaseManager(
            pool_start=config['pool_start'],
            pool_end=config['pool_end'],
            lease_time=int(config['lease_time']),
            raft_cfg=raft_cfg,
            on_change=on_leases_changed,
        )

    return LeaseManager(
        pool_start=config['pool_start'],
        pool_end=config['pool_end'],
        lease_time=int(config['lease_time']),
        on_change=on_leases_changed,
    )


class DHCPServer:
    """
    DHCP server that handles DISCOVER / REQUEST / RELEASE / INFORM.

    Parameters
    ----------
    config : dict
        Runtime configuration (see config.json for field names).
    on_log : Callable[[str], None] | None
        Optional callback receiving human-readable log lines.
    on_leases_changed : Callable | None
        Optional callback invoked whenever the lease table changes.
    """

    def __init__(
        self,
        config: dict,
        on_log: Callable[[str], None] | None = None,
        on_leases_changed: Callable | None = None,
    ):
        self.config           = config
        self._on_log          = on_log
        self._running         = False
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._purge_thread: threading.Thread | None = None

        self.lease_manager = _build_lease_manager(config, on_leases_changed)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._purge_thread = threading.Thread(target=self._purge_loop, daemon=True)
        self._purge_thread.start()
        self._log('DHCP-Server gestartet.')

    def stop(self) -> None:
        self._running = False
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        # Gracefully shutdown the Raft engine if present
        if hasattr(self.lease_manager, 'stop'):
            try:
                self.lease_manager.stop()
            except Exception:
                pass
        self._log('DHCP-Server gestoppt.')

    def update_config(self, config: dict) -> None:
        """Apply new configuration (pool + lease time) without restart."""
        self.config = config
        self.lease_manager.lease_time = int(config['lease_time'])
        self.lease_manager.update_pool(config['pool_start'], config['pool_end'])

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Main server loop
    # ------------------------------------------------------------------

    def _serve(self) -> None:
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._sock.settimeout(1.0)
            bind_ip = self.config.get('bind_ip', '')
            self._sock.bind((bind_ip, SERVER_PORT))
        except OSError as exc:
            self._log(f'FEHLER beim Öffnen von Port {SERVER_PORT}: {exc}')
            self._running = False
            return

        while self._running:
            try:
                data, addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                pkt = DHCPPacket.unpack(data)
                self._handle(pkt, addr)
            except Exception as exc:
                self._log(f'Paketfehler von {addr}: {exc}')

    # ------------------------------------------------------------------
    # Packet dispatch
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Raft leader-check helpers
    # ------------------------------------------------------------------

    def _raft_enabled(self) -> bool:
        return bool(self.config.get('raft_enabled'))

    def _is_raft_leader(self) -> bool:
        """Return True when Raft is either disabled (single-node) or this
        node is the current leader."""
        if not self._raft_enabled():
            return True
        return getattr(self.lease_manager, 'is_leader', True)

    def _allow_follower_renewals(self) -> bool:
        return bool(self.config.get('raft_allow_follower_renewals', False))

    # ------------------------------------------------------------------
    # Packet dispatch
    # ------------------------------------------------------------------

    def _handle(self, pkt: DHCPPacket, addr: tuple) -> None:
        msg_type = pkt.get_message_type()
        if msg_type is None:
            return

        name = MSG_TYPE_NAMES.get(msg_type, f'Typ {msg_type}')
        mac  = pkt.get_mac()
        host = pkt.get_hostname() or '–'
        self._log(f'{name} von {mac}  (Hostname: {host})')

        if msg_type == DHCPDISCOVER:
            self._handle_discover(pkt)
        elif msg_type == DHCPREQUEST:
            self._handle_request(pkt)
        elif msg_type == DHCPRELEASE:
            self._handle_release(pkt)
        elif msg_type == DHCPINFORM:
            self._handle_inform(pkt)
        elif msg_type == DHCPDECLINE:
            self._log(f'DECLINE von {mac} – IP wird gesperrt.')

    # ------------------------------------------------------------------
    # Message handlers
    # ------------------------------------------------------------------

    def _handle_discover(self, pkt: DHCPPacket) -> None:
        # In Raft mode only the leader sends offers for new allocations
        if not self._is_raft_leader():
            self._log(f'DISCOVER von {pkt.get_mac()} ignoriert – kein Leader.')
            return

        mac          = pkt.get_mac()
        requested_ip = pkt.get_requested_ip()
        offered_ip   = self.lease_manager.offer_ip(mac, requested_ip)

        if not offered_ip:
            self._log(f'Kein freies IP für {mac} – Pool erschöpft.')
            return

        reply = self._build_reply(pkt, DHCPOFFER, offered_ip)
        self._send(reply, pkt)
        self._log(f'OFFER  {offered_ip} → {mac}')

    def _handle_request(self, pkt: DHCPPacket) -> None:
        mac          = pkt.get_mac()
        server_id    = pkt.get_server_id()
        my_ip        = self.config.get('server_ip', self._detect_server_ip())

        # If there is a server-identifier option and it is not ours, ignore.
        if server_id and server_id != my_ip:
            return

        requested_ip = pkt.get_requested_ip() or pkt.ciaddr
        hostname     = pkt.get_hostname()

        # ---------- Raft: follower handling ----------
        if self._raft_enabled() and not self._is_raft_leader():
            if self._allow_follower_renewals():
                # Followers may serve renewals for already-known leases.
                existing = self.lease_manager.get_lease(mac)
                if existing and not existing.is_expired:
                    reply = self._build_reply(pkt, DHCPACK, existing.ip)
                    self._send(reply, pkt)
                    self._log(
                        f'ACK (Follower-Renewal) {existing.ip} → {mac}'
                    )
                    return
            # Cannot serve; send NAK so client retries with another server.
            reply = self._build_nak(pkt)
            self._send(reply, pkt)
            self._log(f'NAK (kein Leader) → {mac}')
            return
        # ---------- Normal / leader path ----------

        assigned_ip  = self.lease_manager.assign_ip(mac, hostname, requested_ip or None)

        if not assigned_ip:
            reply = self._build_nak(pkt)
            self._send(reply, pkt)
            self._log(f'NAK → {mac}')
            return

        reply = self._build_reply(pkt, DHCPACK, assigned_ip)
        self._send(reply, pkt)
        self._log(f'ACK    {assigned_ip} → {mac}  (Hostname: {hostname or "–"})')

    def _handle_release(self, pkt: DHCPPacket) -> None:
        mac = pkt.get_mac()
        self.lease_manager.release_ip(mac)
        self._log(f'RELEASE von {mac}')

    def _handle_inform(self, pkt: DHCPPacket) -> None:
        """DHCPINFORM: client already has an IP, only wants options."""
        reply = self._build_reply(pkt, DHCPACK, pkt.ciaddr)
        reply.yiaddr = '0.0.0.0'
        self._send(reply, pkt)
        self._log(f'ACK (INFORM) → {pkt.get_mac()}')

    # ------------------------------------------------------------------
    # Packet builders
    # ------------------------------------------------------------------

    def _build_reply(self, req: DHCPPacket, msg_type: int, ip: str) -> DHCPPacket:
        cfg     = self.config
        my_ip   = cfg.get('server_ip', self._detect_server_ip())
        lease_t = int(cfg['lease_time'])

        reply         = DHCPPacket()
        reply.op      = 2               # BOOTREPLY
        reply.htype   = req.htype
        reply.hlen    = req.hlen
        reply.xid     = req.xid
        reply.flags   = req.flags
        reply.giaddr  = req.giaddr
        reply.chaddr  = req.chaddr
        reply.yiaddr  = ip
        reply.siaddr  = my_ip

        reply.options[OPT_MSG_TYPE]       = bytes([msg_type])
        reply.options[OPT_SERVER_ID]      = socket.inet_aton(my_ip)
        reply.options[OPT_SUBNET_MASK]    = socket.inet_aton(cfg['subnet_mask'])
        reply.options[OPT_LEASE_TIME]     = lease_t.to_bytes(4, 'big')
        reply.options[OPT_RENEWAL_TIME]   = (lease_t // 2).to_bytes(4, 'big')
        reply.options[OPT_REBINDING_TIME] = (lease_t * 7 // 8).to_bytes(4, 'big')

        if cfg.get('router'):
            try:
                reply.options[OPT_ROUTER] = socket.inet_aton(cfg['router'])
            except OSError:
                pass

        dns_servers = [s.strip() for s in cfg.get('dns_servers', '').split(',') if s.strip()]
        if dns_servers:
            dns_bytes = b''
            for dns in dns_servers:
                try:
                    dns_bytes += socket.inet_aton(dns)
                except OSError:
                    pass
            if dns_bytes:
                reply.options[OPT_DNS] = dns_bytes

        return reply

    def _build_nak(self, req: DHCPPacket) -> DHCPPacket:
        my_ip       = self.config.get('server_ip', self._detect_server_ip())
        nak         = DHCPPacket()
        nak.op      = 2
        nak.htype   = req.htype
        nak.hlen    = req.hlen
        nak.xid     = req.xid
        nak.flags   = req.flags
        nak.giaddr  = req.giaddr
        nak.chaddr  = req.chaddr
        nak.options[OPT_MSG_TYPE]  = bytes([DHCPNAK])
        nak.options[OPT_SERVER_ID] = socket.inet_aton(my_ip)
        return nak

    # ------------------------------------------------------------------
    # Send helpers
    # ------------------------------------------------------------------

    def _send(self, reply: DHCPPacket, req: DHCPPacket) -> None:
        if not self._sock:
            return
        data = reply.pack()

        # Unicast if client has an IP; broadcast otherwise (RFC 2131 §4.1)
        if req.giaddr and req.giaddr != '0.0.0.0':
            dest = (req.giaddr, SERVER_PORT)
        elif req.ciaddr and req.ciaddr != '0.0.0.0':
            dest = (req.ciaddr, CLIENT_PORT)
        elif reply.flags & 0x8000:
            dest = ('255.255.255.255', CLIENT_PORT)
        else:
            dest = ('255.255.255.255', CLIENT_PORT)

        try:
            self._sock.sendto(data, dest)
        except OSError as exc:
            self._log(f'Sendefehler: {exc}')

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _detect_server_ip(self) -> str:
        """Best-effort detection of the local IP used for outgoing traffic."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(('8.8.8.8', 80))
                return s.getsockname()[0]
        except OSError:
            return '0.0.0.0'

    def _log(self, msg: str) -> None:
        ts = time.strftime('%H:%M:%S')
        line = f'[{ts}] {msg}'
        logger.info(msg)
        if self._on_log:
            try:
                self._on_log(line)
            except Exception:
                pass

    def _purge_loop(self) -> None:
        """Background thread: remove expired leases every 60 seconds."""
        while self._running:
            time.sleep(60)
            n = self.lease_manager.purge_expired()
            if n:
                self._log(f'{n} abgelaufene Lease(s) entfernt.')
