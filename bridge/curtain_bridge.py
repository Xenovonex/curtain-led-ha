#!/usr/bin/env python3
"""Curtain LED BLE -> MQTT bridge.

Bridges a Zengge / "MagicHome2" style BLE LED curtain (sold under names like
SurpLife, YIQU, etc.; app package com.zennge.magichome2) to Home Assistant over
MQTT. Run this on a machine with Bluetooth that is physically near the light.

Configure via environment variables (see .env.example):
  CURTAIN_ADDR   BLE address of the light, e.g. AA:BB:CC:DD:EE:FF   (required)
  MQTT_HOST      MQTT broker host                 (default 127.0.0.1)
  MQTT_PORT      MQTT broker port                 (default 1883)
  MQTT_USER      MQTT username                    (default "")
  MQTT_PASS      MQTT password                    (default "")
  CURTAIN_NODE   MQTT/HA node id                  (default "curtain")
"""
import asyncio, json, colorsys, os, logging
import paho.mqtt.client as mqtt
from bleak import BleakScanner, BleakClient
from bleak_retry_connector import establish_connection

# ---- config ----
ADDR = os.environ.get("CURTAIN_ADDR", "").strip()
if not ADDR:
    raise SystemExit("Set CURTAIN_ADDR to your light's BLE address (see README).")
MQTT_HOST = os.environ.get("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
NODE = os.environ.get("CURTAIN_NODE", "curtain")

WRITE = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000ff02-0000-1000-8000-00805f9b34fb"
DISCO = f"homeassistant/light/{NODE}/config"
T_CMD = f"{NODE}/set"
T_STATE = f"{NODE}/state"
T_AVAIL = f"{NODE}/availability"
T_RAW = f"{NODE}/raw"

HANDSHAKE = "0a10141a09180f2a1b04000fc6"
QUERY = "0aea818a8b59"

# A few built-in effects exposed to HA's light "effect" list.
EFFECTS = {
    "Rainbow 4":  "0ae1050064010005640000000000000000000000000000000000a100000004a1006464a1126464a11e6464a13c6464",
    "Rainbow 6":  "0ae1050064010206640000000000000000000000000000000000a100000006a1006464a1966464a1786464a15a6464a13c6464a11e6464",
    "Moving Mix": "0ae20baaff",
}

log = logging.getLogger("curtain")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")


class Proto:
    """Wraps a payload in the light's transport frame (see docs/PROTOCOL.md)."""
    def __init__(self): self.seq = 0
    def reset(self): self.seq = 0
    def frame(self, payload_hex):
        self.seq = (self.seq % 255) + 1
        p = bytes.fromhex(payload_hex)
        return bytes([0x01, self.seq, 0x80, 0x00, 0x00, len(p) - 1, 0x00, len(p)]) + p


def power(on): return "0a7123" if on else "0a7124"
def brightness(ha255):
    v = max(0, min(100, round(ha255 * 100 / 255)))
    return f"0ae0020002{v:02x}50"
def color(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    hue = int(h * 255) % 256
    return f"0ae20b{hue:02x}ff"


class Light:
    def __init__(self):
        self.q = asyncio.Queue()
        self.client = None
        self.connected = False
        self.proto = Proto()
        self.state = {"state": "OFF", "brightness": 255, "color": {"r": 255, "g": 255, "b": 255}, "effect": None}
        self.mqtt = None

    async def send(self, payload_hex):
        if not (self.client and self.connected): return
        try:
            await self.client.write_gatt_char(WRITE, self.proto.frame(payload_hex), response=False)
            await asyncio.sleep(0.12)
        except Exception as e:
            log.warning(f"write failed: {e}"); await self._teardown()

    async def _ensure_services(self):
        for _ in range(24):
            try:
                if self.client.services and self.client.services.get_characteristic(WRITE):
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def _teardown(self):
        c = self.client; self.client = None; self.connected = False; self._avail(False)
        try:
            if c: await c.disconnect()
        except Exception:
            pass

    async def _scan(self):
        seen = {}
        def cb(d, a):
            if d.address.upper() == ADDR.upper(): seen["d"] = d
        log.info("scanning for light...")
        s = BleakScanner(detection_callback=cb); await s.start()
        for _ in range(40):
            await asyncio.sleep(0.25)
            if "d" in seen: break
        await s.stop()
        log.info("scan result: %s", "FOUND" if "d" in seen else "not found")
        return seen.get("d")

    async def connect_loop(self):
        backoff = 3
        while True:
            if not self.connected:
                dev = await self._scan()
                if not dev:
                    await asyncio.sleep(backoff); continue
                try:
                    log.info("connecting to light...")
                    self.client = await establish_connection(BleakClient, dev, ADDR, max_attempts=4)
                    if not await self._ensure_services():
                        raise RuntimeError("services not discovered")
                    self.proto.reset()
                    try: await self.client.start_notify(NOTIFY, lambda _, d: None)
                    except Exception: pass
                    await asyncio.sleep(0.3)
                    await self.client.write_gatt_char(WRITE, self.proto.frame(HANDSHAKE), response=False); await asyncio.sleep(0.25)
                    await self.client.write_gatt_char(WRITE, self.proto.frame(QUERY), response=False)
                    self.connected = True; backoff = 3
                    self._avail(True); log.info("CONNECTED to light"); await self._republish()
                except Exception as e:
                    log.warning(f"connect failed: {e}")
                    await self._teardown()
                    await asyncio.sleep(backoff); backoff = min(backoff + 2, 15)
                    continue
            await asyncio.sleep(2)
            if self.connected:
                try:
                    await self.client.write_gatt_char(WRITE, self.proto.frame(QUERY), response=False)
                except Exception as e:
                    log.warning(f"keepalive lost: {e}"); await self._teardown()

    async def worker(self):
        while True:
            cmd = await self.q.get()
            try: await self._apply(cmd)
            except Exception as e: log.warning(f"apply error: {e}")

    async def _apply(self, cmd):
        if "__raw__" in cmd:
            for p in cmd["__raw__"].split(","):
                p = p.strip()
                if p: await self.send(p)
            return
        if "effect" in cmd and cmd["effect"] in EFFECTS:
            await self.send(power(True)); await self.send(EFFECTS[cmd["effect"]])
            self.state["state"] = "ON"; self.state["effect"] = cmd["effect"]
            await self._republish(); return
        if "color" in cmd:
            c = cmd["color"]; r, g, b = c.get("r", 255), c.get("g", 255), c.get("b", 255)
            await self.send(power(True)); await self.send(color(r, g, b))
            self.state["state"] = "ON"; self.state["color"] = {"r": r, "g": g, "b": b}; self.state["effect"] = None
        if "brightness" in cmd:
            await self.send(power(True)); await self.send(brightness(cmd["brightness"]))
            self.state["state"] = "ON"; self.state["brightness"] = cmd["brightness"]
        if "state" in cmd and "color" not in cmd and "brightness" not in cmd and "effect" not in cmd:
            on = cmd["state"] == "ON"
            await self.send(power(on)); self.state["state"] = "ON" if on else "OFF"
        await self._republish()

    def _avail(self, up):
        if self.mqtt: self.mqtt.publish(T_AVAIL, "online" if up else "offline", retain=True)
    async def _republish(self):
        if self.mqtt: self.mqtt.publish(T_STATE, json.dumps(self.state), retain=True)


def main():
    loop = asyncio.new_event_loop(); asyncio.set_event_loop(loop)
    light = Light()

    def on_connect(c, u, f, rc):
        log.info(f"MQTT connected rc={rc}")
        disco = {
            "name": "Curtain Lights", "unique_id": NODE, "schema": "json",
            "command_topic": T_CMD, "state_topic": T_STATE, "availability_topic": T_AVAIL,
            "brightness": True, "supported_color_modes": ["rgb"], "effect": True,
            "effect_list": list(EFFECTS.keys()),
            "device": {"identifiers": [NODE], "name": "Curtain LED", "manufacturer": "Zengge/MagicHome2", "model": "BLE curtain"},
        }
        c.publish(DISCO, json.dumps(disco), retain=True)
        c.subscribe(T_CMD); c.subscribe(T_RAW)

    def on_message(c, u, msg):
        if msg.topic == T_RAW:
            loop.call_soon_threadsafe(light.q.put_nowait, {"__raw__": msg.payload.decode().strip()}); return
        try: cmd = json.loads(msg.payload.decode())
        except Exception: return
        loop.call_soon_threadsafe(light.q.put_nowait, cmd)

    cli = mqtt.Client(client_id=f"{NODE}-bridge")
    if MQTT_USER: cli.username_pw_set(MQTT_USER, MQTT_PASS)
    cli.will_set(T_AVAIL, "offline", retain=True)
    cli.on_connect = on_connect; cli.on_message = on_message
    cli.connect(MQTT_HOST, MQTT_PORT, 60); cli.loop_start()
    light.mqtt = cli

    loop.create_task(light.connect_loop()); loop.create_task(light.worker())
    loop.run_forever()


if __name__ == "__main__":
    main()
