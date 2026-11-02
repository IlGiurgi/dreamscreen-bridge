"""DreamScreen -> WLED bridge.

Pretends to be a DreamScreen SideKick: answers the DreamScreen's subscription
requests (0x010C), receives the 12 sector colours (0x0316) and forwards them to
WLED devices over DDP (UDP 4048). When the stream stops, WLED falls back to its
own effect after its realtime timeout (2.5 s by default).

Sector layout as seen from the sofa (from the DreamScreen V2 UDP protocol doc):

     7   6   5   4   3
     8               2
     9  10  11  12   1

Usage:
    python bridge.py [--config options.json]           # run the bridge
    python bridge.py [--config options.json] --test 10 # fake rainbow, check mapping
"""

import argparse
import colorsys
import json
import socket
import time

DEFAULT_OPTIONS = {
    "dreamscreen_ip": "",   # empty = discover on the network
    "group": 1,             # DreamScreen only streams to SideKicks of its group (not 0)
    "gain": 1.0,            # colour boost, 1.0 = as sent by the DreamScreen
    "smoothing": 0.0,       # 0 = none, 0.9 = very soft transitions
    "fps_log_interval": 60,
    "devices": [
        {"name": "Lampada", "ip": "192.168.86.36", "leds": 60, "mode": "average", "sectors": "9,10,11,12,1"},
        {"name": "Divano", "ip": "192.168.86.25", "leds": 300, "mode": "center"},
        # ESPHome "wled" effect: speaks WLED realtime UDP (DRGB), not DDP
        {"name": "LED Orologio", "ip": "192.168.86.51", "leds": 5, "mode": "average", "protocol": "drgb"},
    ],
}

# Columns from left to right, each averaging its sectors
COLUMNS = [[7, 8, 9], [6, 10], [5, 11], [4, 12], [3, 2, 1]]
# Rings from the centre outwards, for "center" mode
RINGS = [[5, 11], [4, 6, 10, 12], [3, 7, 2, 8, 1, 9]]

DS_PORT = 8888
DDP_PORT = 4048
REALTIME_PORT = 21324


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)

# --- DreamScreen protocol --------------------------------------------------

def _crc8_table():
    table = []
    for i in range(256):
        c = i
        for _ in range(8):
            c = ((c << 1) ^ 0x07) & 0xFF if c & 0x80 else (c << 1) & 0xFF
        table.append(c)
    return table

CRC8 = _crc8_table()

def crc8(data):
    c = 0
    for b in data:
        c = CRC8[(c ^ b) & 0xFF]
    return c

def ds_packet(group, flags, upper, lower, payload=b""):
    p = bytes([0xFC, 5 + len(payload), group, flags, upper, lower]) + payload
    return p + bytes([crc8(p)])

def parse(data):
    """Return (group, flags, upper, lower, payload) or None if invalid."""
    if len(data) < 7 or data[0] != 0xFC or data[1] != len(data) - 2:
        return None
    if crc8(data[:-1]) != data[-1]:
        return None
    return data[2], data[3], data[4], data[5], data[6:-1]

def local_subnet():
    """First three octets of our LAN address, e.g. '192.168.86'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0].rsplit(".", 1)[0]
    finally:
        s.close()

def read_state(sock, ip):
    """Ask for the current state; returns (ip, group, mode, product) or None.

    ip may be a single address or a list (unicast sweep)."""
    for target in [ip] if isinstance(ip, str) else ip:
        sock.sendto(bytes.fromhex("FC05FF30010A2A"), (target, DS_PORT))
    end = time.time() + 3
    while time.time() < end:
        try:
            data, (src, _) = sock.recvfrom(2048)
        except socket.timeout:
            continue
        msg = parse(data)
        if msg and msg[2:4] == (0x01, 0x0A) and len(msg[4]) > 34 and msg[4][-1] in (1, 2):
            p = msg[4]
            return src, p[32], p[33], p[-1]
    return None

# --- colour mapping --------------------------------------------------------

def avg(colours):
    n = len(colours)
    return tuple(sum(c[i] for c in colours) / n for i in range(3))

def lerp(a, b, t):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))

def gradient(stops, t):
    """Colour at position t (0..1) along evenly spaced stops."""
    x = t * (len(stops) - 1)
    k = min(int(x), len(stops) - 2)
    return lerp(stops[k], stops[k + 1], x - k)

def to_bytes(colours, gain):
    return bytes(min(255, int(v * gain)) for c in colours for v in c)

def render(device, sectors, gain):
    """sectors: dict 1..12 -> (r, g, b). Returns raw RGB bytes for the device."""
    n = device["leds"]
    mode = device.get("mode", "spread")
    if mode == "average":
        c = avg([sectors[s] for s in device["_sectors"]])
        return to_bytes([c], gain) * n
    if mode == "center":
        rings = [avg([sectors[s] for s in ring]) for ring in RINGS]
        half = (n - 1) / 2 or 1
        out = [gradient(rings, abs(i - half) / half) for i in range(n)]
    else:  # spread: screen left -> right along the strip
        cols = [avg([sectors[s] for s in col]) for col in COLUMNS]
        out = [gradient(cols, i / max(n - 1, 1)) for i in range(n)]
    if device.get("reverse"):
        out.reverse()
    return to_bytes(out, gain)

# --- DDP -------------------------------------------------------------------

_seq = 0

def send_ddp(sock, ip, rgb):
    global _seq
    _seq = _seq % 15 + 1
    for off in range(0, len(rgb), 1440):
        chunk = rgb[off:off + 1440]
        last = off + 1440 >= len(rgb)
        flags = 0x41 if last else 0x40  # version 1, push on last packet
        header = bytes([flags, _seq, 0x0B, 0x01]) + off.to_bytes(4, "big") + len(chunk).to_bytes(2, "big")
        sock.sendto(header + chunk, (ip, DDP_PORT))

def send_drgb(sock, ip, rgb):
    """WLED realtime UDP, DRGB (protocol 2): up to 490 LEDs, 2 s timeout."""
    sock.sendto(bytes([2, 2]) + rgb[:490 * 3], (ip, REALTIME_PORT))

def send(sock, device, rgb):
    if device.get("protocol") == "drgb":
        send_drgb(sock, device["ip"], rgb)
    else:
        send_ddp(sock, device["ip"], rgb)

# --- main loops ------------------------------------------------------------

def load_options(path):
    opts = dict(DEFAULT_OPTIONS)
    if path:
        with open(path) as f:
            opts.update(json.load(f))
    for dev in opts["devices"]:
        dev["_sectors"] = [int(s) for s in str(dev.get("sectors") or "1,2,3,4,5,6,7,8,9,10,11,12").split(",") if s.strip()]
    return opts

def find_dreamscreen(sock, opts):
    """Locate the DreamScreen and make sure it is in our group."""
    while True:
        target = opts["dreamscreen_ip"] or "255.255.255.255"
        state = read_state(sock, target)
        if not state and not opts["dreamscreen_ip"]:
            # broadcast replies can be filtered: ask every address directly
            net = local_subnet()
            state = read_state(sock, [f"{net}.{i}" for i in range(1, 255)])
        if not state:
            log(f"DreamScreen not found at {target}, retrying in 10 s")
            time.sleep(10)
            continue
        ip, group, mode, product = state
        log(f"DreamScreen {'4K' if product == 2 else 'HD'} at {ip}, group {group}, mode {mode}")
        if group != opts["group"]:
            # group writes are only accepted as broadcast-flagged messages
            sock.sendto(ds_packet(group, 0x21, 0x01, 0x09, bytes([opts["group"]])), (ip, DS_PORT))
            log(f"Moved DreamScreen to group {opts['group']}")
        return ip

def run(opts):
    ds = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    ds.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    ds.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    ds.bind(("", DS_PORT))
    ds.settimeout(1)
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for dev in opts["devices"]:
        log(f"-> {dev['name']} {dev['ip']} ({dev['leds']} LED, {dev.get('mode')})")

    ds_ip = find_dreamscreen(ds, opts)
    group, gain, smoothing = opts["group"], opts["gain"], opts["smoothing"]
    state = None
    frames, t0, last_sub, last_frame, streaming = 0, time.time(), 0, time.time(), False
    while True:
        now = time.time()
        # also subscribe proactively, in case its broadcasts don't reach us
        if now - last_sub >= 2:
            ds.sendto(ds_packet(group, 0x10, 0x01, 0x0C, b"\x01"), (ds_ip, DS_PORT))
            last_sub = now
        if streaming and now - last_frame > 5:
            log("Stream stopped (DreamScreen asleep or ambient mode?)")
            streaming = False
        if now - last_frame > 60:
            # silent for a minute: it may have changed IP or lost its group
            ds_ip, last_frame = find_dreamscreen(ds, opts), time.time()
        try:
            data, (ip, _) = ds.recvfrom(2048)
        except socket.timeout:
            continue
        msg = parse(data)
        if not msg:
            continue
        _, _, upper, lower, payload = msg
        if (upper, lower) == (0x01, 0x0C):
            ds.sendto(ds_packet(group, 0x10, 0x01, 0x0C, b"\x01"), (ip, DS_PORT))
        elif (upper, lower) == (0x03, 0x16) and len(payload) >= 36:
            raw = [payload[i * 3:i * 3 + 3] for i in range(12)]
            if state is None or smoothing <= 0:
                state = [tuple(c) for c in raw]
            else:
                state = [tuple(s[j] * smoothing + c[j] * (1 - smoothing) for j in range(3)) for s, c in zip(state, raw)]
            sectors = {i + 1: state[i] for i in range(12)}
            for dev in opts["devices"]:
                send(out, dev, render(dev, sectors, gain))
            if not streaming:
                log("Streaming")
                streaming = True
            last_frame = now
            frames += 1
            if opts["fps_log_interval"] and now - t0 >= opts["fps_log_interval"]:
                log(f"{frames / (now - t0):.1f} fps")
                frames, t0 = 0, now

def test(opts, seconds):
    """Rainbow around the 12 sectors, rotating, to check the mapping."""
    out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    end = time.time() + seconds
    while time.time() < end:
        shift = time.time() / 4
        sectors = {}
        for s in range(1, 13):
            r, g, b = colorsys.hsv_to_rgb((s / 12 + shift) % 1, 1, 1)
            sectors[s] = (r * 255, g * 255, b * 255)
        for dev in opts["devices"]:
            send(out, dev, render(dev, sectors, opts["gain"]))
        time.sleep(1 / 30)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", help="JSON options file (the add-on uses /data/options.json)")
    ap.add_argument("--test", type=float, help="send a test rainbow for N seconds")
    args = ap.parse_args()
    options = load_options(args.config)
    test(options, args.test) if args.test else run(options)
