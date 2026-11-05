#!/usr/bin/env python3
"""Advertise a smartphone as an AusweisApp remote card reader.

Workaround for phones whose own discovery broadcast never reaches the PC
(e.g. when the phone is a Wi-Fi hotspot or USB tethering).  It feeds a forged
REMOTE_IFD discovery datagram to the local AusweisApp (UDP 127.0.0.1:24727).

The mode is detected automatically each cycle by probing the phone's TLS
server:

* card-reader mode completes a mutual-TLS handshake with this PC's paired
  certificate, so we advertise the real certificate fingerprint with
  pairing=false;
* pairing mode is TLS-PSK and rejects the handshake, so we advertise a fresh
  IFDID with pairing=true.

Discovery is attempted with two strategies:

1. adb (preferred):  the phone's IP / listening port / model are read over
   USB debugging.
2. network (fallback when adb has no device):  the phone is assumed to be the
   default gateway (true for hotspot/USB-tethering) and its ephemeral TCP
   ports are scanned.

Use --pairing to force pairing mode, --no-adb to force the network strategy.
"""
import argparse
import concurrent.futures as cf
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import time

PACKAGE = "com.governikus.ausweisapp2"

ADB = "adb"
IP = "ip"
CONFIG = None
EPHEMERAL = (32768, 60999)
UDP_PORT = 24727
USE_ADB = True
NAME = None
FORCE_PAIRING = False

_ctx = None
_ctx_tried = False
_net_cache = {"ip": None, "port": None}


def run(cmd):
    return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)


# --- config helpers -------------------------------------------------------


def config_text():
    return open(CONFIG, encoding="utf-8").read()


def paired_entries():
    text = config_text()
    if "trustedRemoteInfo=" not in text:
        return []
    line = text.split("trustedRemoteInfo=", 1)[1].splitlines()[0]
    m = re.search(r"\[(.*)\]", line)
    if not m:
        return []
    try:
        return json.loads("[" + m.group(1).replace('\\"', '"') + "]")
    except Exception:
        return []


def name_for_fingerprint(fp):
    for entry in reversed(paired_entries()):
        if entry.get("fingerprint") == fp:
            return entry.get("name")
    return None


def extract_pem(value):
    value = value.strip()
    for prefix in ('"@ByteArray(', "@ByteArray("):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    if value.endswith(')"'):
        value = value[:-2]
    elif value.endswith(")"):
        value = value[:-1]
    return value.replace("\\n", "\n").rstrip() + "\n"


def client_ctx():
    """SSLContext with this PC's paired client certificate, or None."""
    global _ctx, _ctx_tried
    if _ctx_tried:
        return _ctx
    _ctx_tried = True
    text = config_text()
    i = text.find("[remotereader]")
    if i < 0:
        return None
    cert = extract_pem(text[text.find("certificate=", i) + len("certificate="):text.find("key=", i)])
    key = extract_pem(text[text.find("key=", i) + len("key="):])
    if "BEGIN CERTIFICATE" not in cert or "BEGIN" not in key:
        return None
    tmpdir = tempfile.mkdtemp(prefix="ausweis-tls-")
    cert_path = os.path.join(tmpdir, "pc.crt")
    key_path = os.path.join(tmpdir, "pc.key")
    with open(cert_path, "w") as fh:
        fh.write(cert)
    with open(key_path, "w") as fh:
        fh.write(key)
    os.chmod(key_path, 0o600)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.load_cert_chain(cert_path, key_path)
    _ctx = ctx
    return ctx


# --- adb strategy ---------------------------------------------------------


def app_uid():
    out = run([ADB, "shell", "ps", "-A", "-o", "UID,NAME"])
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == PACKAGE:
            return int(parts[0])
    raise RuntimeError("AusweisApp is not running on phone")


def listening_port(uid):
    ports = set()
    for proto in ("tcp", "tcp6"):
        out = run([ADB, "shell", "cat", "/proc/net/%s" % proto])
        for line in out.splitlines()[1:]:
            f = line.split()
            if len(f) < 10 or f[3] != "0A" or int(f[7]) != uid:
                continue
            p = int(f[1].rsplit(":", 1)[1], 16)
            if p:
                ports.add(p)
    if not ports:
        raise RuntimeError("No listening port found for AusweisApp")
    return sorted(ports)[0]


def phone_ips():
    out = run([ADB, "shell", "ip", "-4", "-o", "addr", "show"])
    ips = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            ips.append(ipaddress.ip_interface(parts[parts.index("inet") + 1]))
    return ips


def local_nets():
    out = run([IP, "-4", "-o", "addr", "show"])
    nets = []
    for line in out.splitlines():
        parts = line.split()
        if "inet" in parts:
            nets.append(ipaddress.ip_interface(parts[parts.index("inet") + 1]).network)
    return nets


def pick_phone_ip(nets):
    for iface in phone_ips():
        if iface.ip.is_loopback:
            continue
        if any(iface.ip in net for net in nets):
            return str(iface.ip)
    raise RuntimeError("No phone IP on a network shared with this PC")


def adb_device_info():
    model = run([ADB, "shell", "getprop", "ro.product.model"]).strip()
    uid = app_uid()
    ip = pick_phone_ip(local_nets())
    port = listening_port(uid)
    return model, ip, port


# --- network strategy -----------------------------------------------------


def gateway_ips():
    out = run([IP, "-4", "route", "show"])
    ips = []
    for line in out.splitlines():
        if line.startswith("default via "):
            ip = line.split()[2]
            if ip not in ips:
                ips.append(ip)
    return ips


def scan_open_ports(host):
    lo, hi = EPHEMERAL

    def probe(p):
        sock = socket.socket()
        sock.settimeout(0.4)
        try:
            return p if sock.connect_ex((host, p)) == 0 else None
        finally:
            sock.close()

    open_ports = []
    with cf.ThreadPoolExecutor(max_workers=256) as ex:
        for r in ex.map(probe, range(lo, hi + 1)):
            if r:
                open_ports.append(r)
    return open_ports


def network_ip_port(refresh=False):
    if not refresh and _net_cache["port"]:
        return _net_cache["ip"], _net_cache["port"]
    for ip in gateway_ips():
        for port in scan_open_ports(ip):
            _net_cache["ip"] = ip
            _net_cache["port"] = port
            return ip, port
    raise RuntimeError("no smartphone reader found on any gateway")


# --- mode detection -------------------------------------------------------


def tls_probe(host, port):
    """Probe the reader's TLS server.

    Returns ("cert", fingerprint) in card-reader mode, ("psk", None) in pairing
    mode (or untrusted), or (None, None) if the port is not reachable.
    """
    ctx = client_ctx()
    if ctx is None:
        return ("psk", None)
    try:
        sock = socket.create_connection((host, port), timeout=3)
    except Exception:
        return (None, None)
    try:
        with ctx.wrap_socket(sock, server_hostname="AusweisApp") as ss:
            der = ss.getpeercert(binary_form=True)
            if not der:
                return ("psk", None)
            return ("cert", hashlib.sha256(der).hexdigest())
    except Exception:
        return ("psk", None)


# --- discovery ------------------------------------------------------------


def discover():
    name = NAME
    ip = port = None
    if USE_ADB:
        try:
            model, ip, port = adb_device_info()
            name = NAME or model
        except Exception:
            ip = port = None
    if ip is None or port is None:
        ip, port = network_ip_port()
        name = NAME or "smartphone"
    kind, fp = tls_probe(ip, port)
    if kind is None:
        ip, port = network_ip_port(refresh=True)
        kind, fp = tls_probe(ip, port)
    if kind == "cert":
        return (name_for_fingerprint(fp) or name), ip, port, fp, False
    return name, ip, port, None, True


# --- advertise ------------------------------------------------------------


def build_packet(model, ip, port, ifdid, pairing=False):
    return json.dumps(
        {
            "msg": "REMOTE_IFD",
            "IFDName": model,
            "IFDID": ifdid,
            "port": port,
            "SupportedAPI": ["IFDInterface_WebSocket_v2"],
            "addresses": ["wss://%s:%d" % (ip, port)],
            "pairing": pairing,
        },
        separators=(",", ":"),
    ).encode()


def main():
    global ADB, IP, CONFIG, EPHEMERAL, UDP_PORT, USE_ADB, NAME, FORCE_PAIRING

    parser = argparse.ArgumentParser(prog="ausweis-sak-advertise", description=__doc__.splitlines()[0])
    parser.add_argument("--config", metavar="PATH", default=None,
                        help="AusweisApp config file (default: ~/.config/AusweisApp/AusweisApp2.conf)")
    parser.add_argument("--adb", metavar="PATH", default="adb",
                        help="path to the adb binary (default: adb in PATH)")
    parser.add_argument("--ip", metavar="PATH", default="ip",
                        help="path to the ip binary (default: ip in PATH)")
    parser.add_argument("--no-adb", action="store_true",
                        help="skip the adb strategy and always use the network strategy")
    parser.add_argument("--pairing", action="store_true",
                        help="force pairing mode (don't auto-detect)")
    parser.add_argument("--name", metavar="NAME", default=None,
                        help="device name to advertise (default: auto-detected)")
    parser.add_argument("--port-range", nargs=2, type=int, default=list(EPHEMERAL),
                        metavar=("LO", "HI"), help="ephemeral TCP port range to scan (default: %(default)s)")
    parser.add_argument("--udp-port", type=int, default=UDP_PORT,
                        help="local UDP port AusweisApp listens on (default: %(default)s)")
    args = parser.parse_args()

    ADB = args.adb
    IP = args.ip
    CONFIG = args.config or os.path.expanduser("~/.config/AusweisApp/AusweisApp2.conf")
    EPHEMERAL = tuple(args.port_range)
    UDP_PORT = args.udp_port
    USE_ADB = not args.no_adb
    NAME = args.name
    FORCE_PAIRING = args.pairing

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pairing_ifdid = os.urandom(32).hex()
    sent = 0
    while True:
        try:
            name, ip, port, fp, wants_pairing = discover()
            if FORCE_PAIRING:
                wants_pairing = True
                fp = None
            if wants_pairing:
                ifdid = pairing_ifdid
            else:
                ifdid = fp
            sock.sendto(build_packet(name, ip, port, ifdid, pairing=wants_pairing), ("127.0.0.1", UDP_PORT))
            sent += 1
            if sent % 30 == 0:
                print("advertising %s at wss://%s:%d (pairing=%s)" % (name, ip, port, wants_pairing), flush=True)
            time.sleep(1)
        except Exception as exc:
            print("waiting for smartphone: %s" % exc, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
