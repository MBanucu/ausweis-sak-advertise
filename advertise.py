#!/usr/bin/env python3
"""Advertise a smartphone as an AusweisApp remote card reader.

Workaround for phones whose own discovery broadcast never reaches the PC
(e.g. when the phone is a Wi-Fi hotspot or USB tethering).  It feeds a forged
REMOTE_IFD discovery datagram to the local AusweisApp (UDP 127.0.0.1:24727).

Discovery is attempted with two strategies:

1. adb (preferred):  the phone's IP / listening port / model are read over
   USB debugging.  This is fast and exact.
2. network (fallback when adb has no device):  the phone is assumed to be the
   default gateway (true for hotspot/USB-tethering).  Its ephemeral TCP ports
   are scanned and the AusweisApp TLS server is identified by comparing the
   server certificate fingerprint against the fingerprint stored at pairing
   time in the AusweisApp config.

Use --no-adb to force the network strategy even when adb is connected.
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
PAIRING = False
NAME = None

_ctx = None
_net_cache = {"ip": None, "port": None}
_pair_cache = {"ip": None, "port": None}


def run(cmd):
    return subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)


# --- config helpers -------------------------------------------------------


def config_text():
    return open(CONFIG, encoding="utf-8").read()


def paired_info():
    """Return (name, fingerprint) of the most recently paired device."""
    text = config_text()
    line = text.split("trustedRemoteInfo=", 1)[1].splitlines()[0]
    m = re.search(r"\[(.*)\]", line)
    if not m:
        raise RuntimeError("No paired device in AusweisApp config")
    entries = json.loads("[" + m.group(1).replace('\\"', '"') + "]")
    if not entries:
        raise RuntimeError("No paired device in AusweisApp config")
    entry = entries[-1]
    return entry.get("name", "smartphone"), entry["fingerprint"]


def fingerprint_for(model):
    text = config_text()
    line = text.split("trustedRemoteInfo=", 1)[1].splitlines()[0]
    m = re.search(r"\[(.*)\]", line)
    if not m:
        raise RuntimeError("No paired device in AusweisApp config")
    entries = json.loads("[" + m.group(1).replace('\\"', '"') + "]")
    for entry in reversed(entries):
        if entry.get("name") == model:
            return entry["fingerprint"]
    if entries:
        return entries[-1]["fingerprint"]
    raise RuntimeError("No paired device in AusweisApp config")


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
    """SSLContext loaded with this PC's own paired client certificate."""
    global _ctx
    if _ctx is not None:
        return _ctx
    text = config_text()
    i = text.find("[remotereader]")
    cert = extract_pem(text[text.find("certificate=", i) + len("certificate="):text.find("key=", i)])
    key = extract_pem(text[text.find("key=", i) + len("key="):])
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


def discover_via_adb():
    model, ip, port = adb_device_info()
    return model, ip, port, fingerprint_for(model)


def discover_for_pairing():
    if USE_ADB:
        try:
            model, ip, port = adb_device_info()
            return NAME or model, ip, port
        except Exception:
            pass
    if _pair_cache["port"]:
        return NAME or "smartphone", _pair_cache["ip"], _pair_cache["port"]
    for ip in gateway_ips():
        for port in scan_open_ports(ip):
            _pair_cache["ip"] = ip
            _pair_cache["port"] = port
            return NAME or "smartphone", ip, port
    raise RuntimeError("no smartphone reader found for pairing")


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


def tls_matches(host, port, fp):
    try:
        sock = socket.create_connection((host, port), timeout=3)
        with client_ctx().wrap_socket(sock, server_hostname="AusweisApp") as ss:
            der = ss.getpeercert(binary_form=True)
            return bool(der) and hashlib.sha256(der).hexdigest() == fp
    except Exception:
        return False


def discover_via_network():
    name, fp = paired_info()
    for ip in gateway_ips():
        if _net_cache["ip"] == ip and _net_cache["port"]:
            if tls_matches(ip, _net_cache["port"], fp):
                return name, ip, _net_cache["port"], fp
            _net_cache["port"] = None
        for port in scan_open_ports(ip):
            if tls_matches(ip, port, fp):
                _net_cache["ip"] = ip
                _net_cache["port"] = port
                return name, ip, port, fp
    raise RuntimeError("AusweisApp reader not found on any gateway")


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
    global ADB, IP, CONFIG, EPHEMERAL, UDP_PORT, USE_ADB, PAIRING, NAME

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
                        help="advertise in pairing mode (phone shows a pairing code)")
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
    PAIRING = args.pairing
    NAME = args.name

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    pairing_ifdid = os.urandom(32).hex()
    sent = 0
    while True:
        try:
            if PAIRING:
                name, ip, port = discover_for_pairing()
                ifdid, pairing = pairing_ifdid, True
            elif USE_ADB:
                try:
                    name, ip, port, ifdid = discover_via_adb()
                except Exception:
                    name, ip, port, ifdid = discover_via_network()
                pairing = False
            else:
                name, ip, port, ifdid = discover_via_network()
                pairing = False
            sock.sendto(build_packet(name, ip, port, ifdid, pairing=pairing), ("127.0.0.1", UDP_PORT))
            sent += 1
            if sent % 30 == 0:
                print("advertising %s at wss://%s:%d (pairing=%s)" % (name, ip, port, pairing), flush=True)
            time.sleep(1)
        except Exception as exc:
            print("waiting for smartphone: %s" % exc, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
