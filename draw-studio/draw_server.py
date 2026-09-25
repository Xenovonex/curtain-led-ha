#!/usr/bin/env python3
"""Curtain LED Studio web server.

Serves index.html (a 20x20 paint/animation studio) and turns grids into the
light's per-pixel `e2 06` draw commands, published to the MQTT raw topic that
the bridge listens on. Persistent MQTT connection + a coalescing rate-limit so
fast animations can never flood the bridge.

Configure via environment variables (see .env.example):
  MQTT_HOST/MQTT_PORT/MQTT_USER/MQTT_PASS   MQTT broker
  CURTAIN_NODE   node id (must match the bridge)   (default "curtain")
  DRAW_PORT      HTTP port                          (default 8095)
  GRID_W/GRID_H  grid size                          (default 20x20)
"""
import http.server, socketserver, json, os, threading, time
import paho.mqtt.client as mqtt

MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
NODE = os.environ.get("CURTAIN_NODE", "curtain")
PORT = int(os.environ.get("DRAW_PORT", "8095"))
W = int(os.environ.get("GRID_W", "20"))
H = int(os.environ.get("GRID_H", "20"))
N = W * H
BYTES = (N + 7) // 8
TOPIC = f"{NODE}/raw"
MIN_INTERVAL = 0.45  # seconds between publishes (roughly matches BLE capacity)

cli = mqtt.Client(client_id=f"{NODE}-draw")
if MQTT_USER: cli.username_pw_set(MQTT_USER, MQTT_PASS)
cli.reconnect_delay_set(min_delay=1, max_delay=10)
try: cli.connect(MQTT_HOST, MQTT_PORT, 60)
except Exception as e: print("mqtt connect err", e)
cli.loop_start()

_latest = {"payload": None}
_lock = threading.Lock()

def _worker():
    last_sent = None; last_pub = 0.0
    while True:
        time.sleep(0.05)
        with _lock: p = _latest["payload"]
        if p is not None and p != last_sent and (time.time() - last_pub) >= MIN_INTERVAL:
            try: cli.publish(TOPIC, p)
            except Exception: pass
            last_sent = p; last_pub = time.time()
threading.Thread(target=_worker, daemon=True).start()

def bitmap(indices):
    b = bytearray(BYTES)
    for i in indices: b[i // 8] |= (0x80 >> (i % 8))
    return b.hex()

def encode(grid, bright=100):
    """grid: list of N values, each None(off) / int hue(0-255) / 'w'(white)."""
    from collections import defaultdict
    val = max(0, min(0x64, round(bright * 0x64 / 100)))
    groups = defaultdict(list)
    for i, v in enumerate(grid):
        if v is None: continue
        r, c = divmod(i, W)
        groups[v].append(c * H + r)      # column-major curtain: transpose
    cmds = ["0ae00e01", "0ae2060000" + "00" + bitmap(range(N))]  # enter draw + clear
    for v, idxs in groups.items():
        if v == "w": cmds.append("0ae2060000%02x%s" % (val, bitmap(idxs)))
        else:        cmds.append("0ae206%02x64%02x%s" % (int(v), val, bitmap(idxs)))
    return ",".join(cmds)

class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
        try: html = open(path, "rb").read()
        except Exception: html = b"<html><body>index.html missing</body></html>"
        self.send_response(200); self.send_header("Content-Type", "text/html")
        self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(html)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n))
            with _lock: _latest["payload"] = encode(body["grid"], body.get("bright", 100))
            self.send_response(200)
        except Exception:
            self.send_response(500)
        self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")

socketserver.ThreadingTCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
    print(f"curtain draw studio on :{PORT}")
    httpd.serve_forever()
