"""DHCP packet parsing and building (RFC 2131 / RFC 2132)."""

import socket
import struct

MAGIC_COOKIE = b'\x63\x82\x53\x63'

# DHCP message types (option 53)
DHCPDISCOVER = 1
DHCPOFFER    = 2
DHCPREQUEST  = 3
DHCPDECLINE  = 4
DHCPACK      = 5
DHCPNAK      = 6
DHCPRELEASE  = 7
DHCPINFORM   = 8

MSG_TYPE_NAMES = {
    DHCPDISCOVER: 'DISCOVER',
    DHCPOFFER:    'OFFER',
    DHCPREQUEST:  'REQUEST',
    DHCPDECLINE:  'DECLINE',
    DHCPACK:      'ACK',
    DHCPNAK:      'NAK',
    DHCPRELEASE:  'RELEASE',
    DHCPINFORM:   'INFORM',
}

# Commonly used option codes
OPT_SUBNET_MASK    = 1
OPT_ROUTER         = 3
OPT_DNS            = 6
OPT_HOSTNAME       = 12
OPT_REQUESTED_IP   = 50
OPT_LEASE_TIME     = 51
OPT_MSG_TYPE       = 53
OPT_SERVER_ID      = 54
OPT_PARAM_LIST     = 55
OPT_RENEWAL_TIME   = 58
OPT_REBINDING_TIME = 59


class DHCPPacket:
    """Represents a single DHCP packet."""

    def __init__(self):
        self.op     = 1           # 1=BOOTREQUEST, 2=BOOTREPLY
        self.htype  = 1           # hardware type: 1=Ethernet
        self.hlen   = 6           # hardware address length
        self.hops   = 0
        self.xid    = 0           # transaction ID
        self.secs   = 0
        self.flags  = 0
        self.ciaddr = '0.0.0.0'   # client IP
        self.yiaddr = '0.0.0.0'   # "your" IP (offered/assigned)
        self.siaddr = '0.0.0.0'   # server IP
        self.giaddr = '0.0.0.0'   # gateway/relay IP
        self.chaddr = b'\x00' * 16
        self.sname  = b'\x00' * 64
        self.file   = b'\x00' * 128
        self.options: dict[int, bytes] = {}

    # ------------------------------------------------------------------
    # Deserialization
    # ------------------------------------------------------------------

    @classmethod
    def unpack(cls, data: bytes) -> 'DHCPPacket':
        """Parse raw bytes into a DHCPPacket."""
        if len(data) < 240:
            raise ValueError('Packet too short to be a valid DHCP packet')

        pkt = cls()
        pkt.op, pkt.htype, pkt.hlen, pkt.hops = struct.unpack_from('!BBBB', data, 0)
        pkt.xid                                = struct.unpack_from('!I',    data, 4)[0]
        pkt.secs, pkt.flags                    = struct.unpack_from('!HH',   data, 8)
        pkt.ciaddr = socket.inet_ntoa(data[12:16])
        pkt.yiaddr = socket.inet_ntoa(data[16:20])
        pkt.siaddr = socket.inet_ntoa(data[20:24])
        pkt.giaddr = socket.inet_ntoa(data[24:28])
        pkt.chaddr = data[28:44]
        pkt.sname  = data[44:108]
        pkt.file   = data[108:236]

        options_data = data[236:]
        if len(options_data) < 4 or options_data[:4] != MAGIC_COOKIE:
            return pkt

        idx = 4
        while idx < len(options_data):
            code = options_data[idx]
            idx += 1
            if code == 0:    # padding
                continue
            if code == 255:  # end
                break
            if idx >= len(options_data):
                break
            length = options_data[idx]
            idx += 1
            pkt.options[code] = options_data[idx: idx + length]
            idx += length

        return pkt

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def pack(self) -> bytes:
        """Serialize the packet to bytes."""
        data  = struct.pack('!BBBB', self.op, self.htype, self.hlen, self.hops)
        data += struct.pack('!I',    self.xid)
        data += struct.pack('!HH',   self.secs, self.flags)
        data += socket.inet_aton(self.ciaddr)
        data += socket.inet_aton(self.yiaddr)
        data += socket.inet_aton(self.siaddr)
        data += socket.inet_aton(self.giaddr)
        data += (self.chaddr + b'\x00' * 16)[:16]
        data += (self.sname  + b'\x00' * 64)[:64]
        data += (self.file   + b'\x00' * 128)[:128]

        # Options
        data += MAGIC_COOKIE
        for code, value in self.options.items():
            data += bytes([code, len(value)]) + value
        data += bytes([255])  # end option

        # RFC 2131 §2: minimum packet size is 300 bytes
        if len(data) < 300:
            data += b'\x00' * (300 - len(data))

        return data

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def get_mac(self) -> str:
        """Return the client MAC address as a colon-separated string."""
        length = max(1, min(self.hlen, 16))
        return ':'.join(f'{b:02x}' for b in self.chaddr[:length])

    def get_message_type(self) -> int | None:
        """Return the DHCP message type or None if not present."""
        raw = self.options.get(OPT_MSG_TYPE)
        return raw[0] if raw else None

    def get_requested_ip(self) -> str | None:
        """Return the requested IP address option value, or None."""
        raw = self.options.get(OPT_REQUESTED_IP)
        return socket.inet_ntoa(raw) if raw and len(raw) == 4 else None

    def get_hostname(self) -> str:
        """Return the hostname option value, or empty string."""
        raw = self.options.get(OPT_HOSTNAME)
        return raw.decode('ascii', errors='replace') if raw else ''

    def get_server_id(self) -> str | None:
        """Return the server identifier option value, or None."""
        raw = self.options.get(OPT_SERVER_ID)
        return socket.inet_ntoa(raw) if raw and len(raw) == 4 else None
