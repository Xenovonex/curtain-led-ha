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
  CURTAIN_LOG_DIR       where "Record BLE log" writes captures  (default ~/curtain-ble-logs)
  CURTAIN_RECORD_MINUTES  auto-stop for a recording, minutes    (default 20)
"""
import asyncio, json, colorsys, os, logging, time
from datetime import datetime
from pathlib import Path
import numpy as np
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
LOG_DIR = Path(os.environ.get("CURTAIN_LOG_DIR", str(Path.home() / "curtain-ble-logs")))
RECORD_AUTOSTOP = int(os.environ.get("CURTAIN_RECORD_MINUTES", "20")) * 60

# ---- sound-reactive mode (local mic -> e20a spectrum stream) ----
# The vendor "DJ" sound mode is NOT a single command: the app streams a frame of
# 20 spectrum-band heights (0ae20a + 20 bytes, 0-100 each) ~8-10x/sec computed
# from a mic. We reproduce it here from a local capture device.
# CURTAIN_MIC_PCM must be an ALSA capture device this host can open concurrently
# with anything else using the mic (e.g. an ALSA dsnoop PCM). Empty => disabled.
MIC_PCM = os.environ.get("CURTAIN_MIC_PCM", "").strip()
MIC_RATE = int(os.environ.get("CURTAIN_MIC_RATE", "48000"))
# Read one FPS-frame per capture. For smooth (non-bursty) delivery, pick an FPS so
# MIC_RATE//FPS equals the capture device's ALSA period_size (e.g. 48000/8 = 6000).
MIC_FPS = int(os.environ.get("CURTAIN_MIC_FPS", "8"))
MIC_BANDS = 20
MIC_FMIN, MIC_FMAX = 100.0, 8000.0   # skip mains hum
MIC_ATTACK, MIC_DECAY = 0.7, 0.45    # per-band smoothing (rise fast, fall snappy)
MIC_PEAK_DECAY = 0.99
MIC_MIN_PEAK = 500.0                 # AGC floor so ambient noise maps near 0
MIC_NOISE_MARGIN = 1.6               # subtract this * per-band running noise floor
MIC_FLOOR_RISE = 0.002               # how fast the per-band noise floor creeps up
MIC_GAMMA = 0.7                      # <1 boosts low-level detail

WRITE = "0000ff01-0000-1000-8000-00805f9b34fb"
NOTIFY = "0000ff02-0000-1000-8000-00805f9b34fb"
DISCO = f"homeassistant/light/{NODE}/config"
DISCO_LINK = f"homeassistant/switch/{NODE}_link/config"
DISCO_REC = f"homeassistant/switch/{NODE}_record/config"
DISCO_REC_FILE = f"homeassistant/sensor/{NODE}_record_file/config"
DISCO_MIC = f"homeassistant/switch/{NODE}_mic/config"
DISCO_SOUND = f"homeassistant/select/{NODE}_sound/config"
DISCO_SENS = f"homeassistant/number/{NODE}_sensitivity/config"
DISCO_TEST = f"homeassistant/button/{NODE}_soundtest/config"
DISCO_TESTST = f"homeassistant/sensor/{NODE}_soundtest/config"
DISCO_HEART = f"homeassistant/switch/{NODE}_heart/config"
DISCO_SMOKE = f"homeassistant/button/{NODE}_smoke/config"
DISCO_SMOKEST = f"homeassistant/sensor/{NODE}_smoke/config"
T_CMD = f"{NODE}/set"
T_STATE = f"{NODE}/state"
T_AVAIL = f"{NODE}/availability"
T_RAW = f"{NODE}/raw"
T_LINK_CMD = f"{NODE}/link/set"      # "ON"/"OFF": hold or release the BLE link
T_LINK_STATE = f"{NODE}/link/state"
T_REC_CMD = f"{NODE}/record/set"     # "ON"/"OFF": start/stop a BLE capture
T_REC_STATE = f"{NODE}/record/state"
T_REC_FILE = f"{NODE}/record/file"   # current/last capture filename
T_MIC_CMD = f"{NODE}/mic/set"        # "ON"/"OFF": local-mic sound-reactive mode
T_MIC_STATE = f"{NODE}/mic/state"
T_SOUND_CMD = f"{NODE}/sound/set"    # which sound-reactive style to show
T_SOUND_STATE = f"{NODE}/sound/state"
T_SENS_CMD = f"{NODE}/sensitivity/set"   # 0-100: how much sound is needed
T_SENS_STATE = f"{NODE}/sensitivity/state"
T_TEST_CMD = f"{NODE}/soundtest/set"     # press -> cycle all style bytes 1-15
T_TEST_STATE = f"{NODE}/soundtest/state"
T_HEART_CMD = f"{NODE}/heart/set"        # ON/OFF: color-cycling disco heart
T_HEART_STATE = f"{NODE}/heart/state"
T_SMOKE_CMD = f"{NODE}/smoke/set"        # press -> run the visual smoke test
T_SMOKE_STATE = f"{NODE}/smoke/state"

HANDSHAKE = "0a10141a09180f2a1b04000fc6"
QUERY = "0aea818a8b59"

# A few built-in effects exposed to HA's light "effect" list.
EFFECTS = {
    "Rainbow 4":  "0ae1050064010005640000000000000000000000000000000000a100000004a1006464a1126464a11e6464a13c6464",
    "Rainbow 6":  "0ae1050064010206640000000000000000000000000000000000a100000006a1006464a1966464a1786464a15a6464a13c6464a11e6464",
    "Moving Mix": "0ae20baaff",
}
# Built-in device animations: 0a e0 02 00 <id> <speed> <bright>. ids 1-17 seen in
# the vendor app; speed/bright default to 0x50/0x64. Exposed to HA's effect list.
for _i in range(1, 18):
    EFFECTS[f"Animation {_i:02d}"] = f"0ae00200{_i:02x}5064"

# Sound-reactive animation STYLES: an e1 05 frame with a style byte + palette that
# sets the *look*, while the e20a mic stream drives the reactivity. Which style
# bytes render varies by firmware (some show nothing), so expose 1-15 to try.
# e1 05 style frame: 0a e1 05 00 50 <style> 00 00 <sensitivity> <pad> a1 <palette>.
# byte9 = sensitivity (0-100): how much sound is needed before the animation reacts.
_SS_TAIL = "0000000000000000000000000000000000a100000006a1006464a1966464a1786464a15a6464a13c6464a11e6464"
def sound_frame(style_n, sens):
    sens = max(0, min(100, int(sens)))
    return f"0ae1050050{style_n:02x}0000{sens:02x}" + _SS_TAIL
# Only style bytes that actually render (verified on hardware + app capture):
#   spectrum/bars: 1-4   |   level/glitter: 7, 8, 12, 13
SOUND_STYLE_NUMS = [1, 2, 3, 4, 7, 8, 12, 13]
SOUND_STYLES = [f"Style {_n:02d}" for _n in SOUND_STYLE_NUMS]   # HA select option names
SOUND_DEFAULT = "Style 03"
SOUND_SENS_DEFAULT = 100
# Which mic-data stream each style reacts to (both computed from the local mic):
#   styles 1-4  -> e2 0a  (20-band spectrum, "bars")
#   styles 5-15 -> e1 07  (single overall level byte, "glitter"/level animations)
SOUND_SPECTRUM_MAX = 4

def _style_num(name):
    try: return int(str(name).split()[-1])
    except Exception: return 0

# Disco-heart: a 20x20 heart bitmap (column-major, bit=col*20+row, MSB-first) drawn
# with 0a e2 06 <hue> <sat> <val> <bitmap>, re-drawn with a rotating hue for a
# smooth color-cycling ("disco") heart. Enter draw mode first with 0a e0 0e 01.
HEART_BITMAP = "000000000007f000ffc00ffe01fff00fff807ffc03ffc00ffe00ffe03ffc07ffc0fff81fff00ffe00ffc007f000000000000"
# Draw frames are large (58 bytes); a high rate saturates the BLE link and drops it,
# locking out other commands. ~1.5 fps keeps the colour-cycle smooth but leaves the
# link responsive. Bigger hue step keeps the cycle lively at the low rate.
HEART_FPS = 1.5
HEART_HUE_STEP = 16

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
        self.link_enabled = True   # False => release the BLE link so a phone app can connect
        self.proto = Proto()
        self.state = {"state": "OFF", "brightness": 255, "color": {"r": 255, "g": 255, "b": 255}, "effect": None}
        self.mqtt = None
        # --- BLE capture ("Record BLE log") ---
        self.rec_fh = None
        self.rec_path = None
        self.rec_task = None
        # --- sound-reactive mic mode ---
        self.mic_enabled = False
        self.mic_task = None
        self.mic_proc = None
        self.sound_style = SOUND_DEFAULT
        self.sound_sensitivity = SOUND_SENS_DEFAULT
        self.cycle_task = None
        self.cycle_status = "idle"
        self.heart_enabled = False
        self.heart_task = None
        self.smoke_task = None
        self.smoke_status = "idle"
        self._mic_win = None
        self._mic_edges = None
        self._mic_floor = None
        self._mic_peak = 1.0
        self._mic_vals = np.zeros(MIC_BANDS, dtype=np.float32)

    # -------- BLE capture --------
    def _rec_write(self, direction, raw_bytes):
        """Append one line to the active capture, if any. Format:
        <epoch_seconds> <W|N> <hex>  (W = we wrote it, N = notification from light)."""
        if not self.rec_fh: return
        try:
            self.rec_fh.write(f"{time.time():.6f} {direction} {raw_bytes.hex()}\n")
            # Do NOT flush every frame: a synchronous disk flush at the ~10 Hz sound
            # stream rate stalls the event loop and drops the BLE link. Flush only
            # occasionally; stop_record() flushes+closes at the end.
            self._rec_n = getattr(self, "_rec_n", 0) + 1
            if self._rec_n % 40 == 0:
                self.rec_fh.flush()
        except Exception as e:
            log.warning(f"record write failed: {e}")

    def _on_notify(self, _sender, data):
        # Notifications carry the light's echoed mode/state - the useful signal
        # when watching what a phone app does (if the light allows a 2nd link).
        self._rec_write("N", bytes(data))

    def start_record(self):
        if self.recording: self._rec_publish(); return
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            name = f"ble-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
            self.rec_path = LOG_DIR / name
            self.rec_fh = open(self.rec_path, "w", encoding="ascii")
            self.rec_fh.write(f"# curtain BLE capture  addr={ADDR}  start={datetime.now().isoformat()}\n")
            self.rec_fh.write("# fields: <epoch> <W|N> <hex>   frame = 01 seq 80 00 00 len-1 00 len <payload>\n")
            self.rec_fh.flush()
            log.info(f"RECORDING BLE to {self.rec_path} (auto-stop in {RECORD_AUTOSTOP//60} min)")
            self.rec_task = asyncio.get_running_loop().create_task(self._rec_autostop())
        except Exception as e:
            log.warning(f"could not start recording: {e}"); self.rec_fh = None; self.rec_path = None
        self._rec_publish()

    def stop_record(self, reason="manual"):
        if self.rec_task:
            self.rec_task.cancel(); self.rec_task = None
        if self.rec_fh:
            try:
                self.rec_fh.write(f"# stopped ({reason}) {datetime.now().isoformat()}\n"); self.rec_fh.close()
            except Exception: pass
            log.info(f"stopped BLE recording ({reason}): {self.rec_path}")
        self.rec_fh = None
        self._rec_publish()

    async def _rec_autostop(self):
        try:
            await asyncio.sleep(RECORD_AUTOSTOP)
            self.stop_record("auto-stop")
        except asyncio.CancelledError:
            pass

    @property
    def recording(self):
        return self.rec_fh is not None

    def _rec_publish(self):
        if not self.mqtt: return
        self.mqtt.publish(T_REC_STATE, "ON" if self.recording else "OFF", retain=True)
        self.mqtt.publish(T_REC_FILE, self.rec_path.name if self.rec_path else "none", retain=True)

    async def send(self, payload_hex, pace=True):
        if not (self.client and self.connected): return
        try:
            frame = self.proto.frame(payload_hex)
            await self.client.write_gatt_char(WRITE, frame, response=False)
            # skip logging the high-rate mic stream frames (own noise; adds load)
            if not payload_hex.startswith(("0ae20a", "0ae107")):
                self._rec_write("W", frame)
            if pace:
                await asyncio.sleep(0.12)
        except Exception as e:
            log.warning(f"write failed: {e}"); await self._teardown()

    # -------- sound-reactive mic mode --------
    def _mic_state(self):
        if self.mqtt: self.mqtt.publish(T_MIC_STATE, "ON" if self.mic_enabled else "OFF", retain=True)

    def _sound_state(self):
        if self.mqtt: self.mqtt.publish(T_SOUND_STATE, self.sound_style, retain=True)

    def _sensitivity_state(self):
        if self.mqtt: self.mqtt.publish(T_SENS_STATE, str(self.sound_sensitivity), retain=True)

    def _spectrum_mode(self):
        """True -> stream e2 0a (20-band, styles 1-4); False -> e1 07 (level, 5+)."""
        return 1 <= _style_num(self.sound_style) <= SOUND_SPECTRUM_MAX

    async def _start_stream(self):
        if self.mic_task is None or self.mic_task.done():
            self.mic_task = asyncio.get_running_loop().create_task(self._mic_loop())

    async def _stop_stream(self):
        if self.mic_task:
            self.mic_task.cancel(); self.mic_task = None
        await self._mic_stop_proc()

    async def _apply_sound(self):
        """Send the selected style frame (with current sensitivity) and keep the
        local-mic stream running. The loop streams e2 0a or e1 07 per style."""
        if self.connected:
            await self.send(sound_frame(_style_num(self.sound_style), self.sound_sensitivity))
        if MIC_PCM:
            await self._start_stream()

    async def set_sound_style(self, name):
        if name not in SOUND_STYLES:
            return
        self._cancel_cycle()
        self.sound_style = name
        log.info(f"sound style -> {name}")
        if self.mic_enabled:
            await self._apply_sound()
        self._sound_state()

    # -------- test cycle: step through all style bytes 1-15, 10s each --------
    def _cycle_state(self):
        if self.mqtt: self.mqtt.publish(T_TEST_STATE, self.cycle_status, retain=True)

    def _cancel_cycle(self):
        if self.cycle_task and not self.cycle_task.done():
            self.cycle_task.cancel()
        self.cycle_task = None

    async def start_cycle(self):
        if self.cycle_task and not self.cycle_task.done():
            return
        self.cycle_task = asyncio.get_running_loop().create_task(self._cycle_loop())

    async def _cycle_loop(self):
        try:
            if not self.mic_enabled:
                await self.set_mic(True)
            for n in range(1, 16):
                self.cycle_status = f"Style {n:02d}  ({n}/15)"; self._cycle_state()
                log.info("sound test -> style %d", n)
                self.sound_style = f"Style {n:02d}"   # drives spectrum-vs-level choice
                if self.connected:
                    await self.send(sound_frame(n, self.sound_sensitivity))
                await asyncio.sleep(10)
            self.cycle_status = "done"; self._cycle_state()
        except asyncio.CancelledError:
            self.cycle_status = "stopped"; self._cycle_state()
        finally:
            self.sound_style = SOUND_DEFAULT; self._sound_state()

    # -------- disco heart (color-cycling pixel-drawn heart) --------
    def _heart_state(self):
        if self.mqtt: self.mqtt.publish(T_HEART_STATE, "ON" if self.heart_enabled else "OFF", retain=True)

    async def set_heart(self, enabled):
        enabled = bool(enabled)
        if enabled == self.heart_enabled:
            self._heart_state(); return
        self.heart_enabled = enabled
        if enabled:
            log.info("Disco heart ON")
            if self.mic_enabled: await self.set_mic(False)   # mutually exclusive
            self._cancel_cycle()
            self.heart_task = asyncio.get_running_loop().create_task(self._heart_loop())
        else:
            log.info("Disco heart OFF")
            if self.heart_task:
                self.heart_task.cancel(); self.heart_task = None
            await self.send(power(True)); await self.send(EFFECTS["Moving Mix"])
        self._heart_state()

    async def _heart_loop(self):
        hue = 0
        try:
            if self.connected:
                await self.send("0ae00e01")   # enter draw mode
            frame = 0
            while self.heart_enabled:
                if self.connected:
                    if frame % 20 == 0:       # periodically re-assert draw mode
                        await self.send("0ae00e01")
                    await self.send(f"0ae206{hue:02x}6464{HEART_BITMAP}", pace=False)
                hue = (hue + HEART_HUE_STEP) % 180
                frame += 1
                await asyncio.sleep(1.0 / HEART_FPS)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning(f"heart loop error: {e}")

    # -------- visual smoke test (runs the whole feature set for a camera check) --------
    def _smoke_state(self):
        if self.mqtt: self.mqtt.publish(T_SMOKE_STATE, self.smoke_status, retain=True)

    async def start_smoke(self):
        if self.smoke_task and not self.smoke_task.done():
            return
        self.smoke_task = asyncio.get_running_loop().create_task(self._smoke_test())

    async def _smoke_test(self):
        def st(s): self.smoke_status = s; self._smoke_state(); log.info("smoke: %s", s)
        noise = None
        try:
            st("1/6 colours")
            await self.send(power(True)); await asyncio.sleep(1)
            for hue in ("00", "55", "aa"):
                await self.send(f"0ae20b{hue}ff"); await asyncio.sleep(3)
            st("2/6 animation")
            await self.send("0ae00200023264"); await asyncio.sleep(9)
            st("3/6 disco heart")
            await self.set_heart(True); await asyncio.sleep(16)
            st("4/6 heart off -> white")
            await self.set_heart(False); await asyncio.sleep(1)
            await self.send("0ae20b0060"); await asyncio.sleep(4)
            st("5/6 sound bars + noise")
            await self.set_sensitivity(100)
            await self.set_mic(True); await self.set_sound_style("Style 03"); await asyncio.sleep(1)
            try:
                noise = await asyncio.create_subprocess_exec(
                    "speaker-test", "-D", "pipewire", "-t", "pink", "-c", "2",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            except Exception as e:
                log.warning("smoke noise failed: %s", e)
            await asyncio.sleep(10)
            if noise and noise.returncode is None:
                noise.terminate()
            await self.set_mic(False); await asyncio.sleep(2)
            st("6/6 scroll stored image")
            await self.send("0ae205020000000000000000001e195a03"); await asyncio.sleep(12)
            await self.send("0ae20b1eff")
            st("done")
        except asyncio.CancelledError:
            st("stopped")
        except Exception as e:
            log.warning("smoke test error: %s", e); st("error")
        finally:
            if noise and noise.returncode is None:
                try: noise.terminate()
                except Exception: pass

    async def set_sensitivity(self, val):
        try:
            self.sound_sensitivity = max(0, min(100, int(float(val))))
        except Exception:
            return
        log.info(f"sound sensitivity -> {self.sound_sensitivity}")
        if self.mic_enabled and self.connected:
            await self.send(sound_frame(_style_num(self.sound_style), self.sound_sensitivity))
        self._sensitivity_state()

    async def set_mic(self, enabled):
        enabled = bool(enabled)
        if enabled == self.mic_enabled:
            self._mic_state(); return
        self.mic_enabled = enabled
        if enabled:
            log.info("MIC sound-reactive mode ON")
            if self.heart_enabled: await self.set_heart(False)   # mutually exclusive
            await self._apply_sound()
        else:
            log.info("MIC sound-reactive mode OFF")
            self._cancel_cycle()
            await self._stop_stream()
            await self.send(power(True)); await self.send(EFFECTS["Moving Mix"])
        self._mic_state()

    async def _mic_stop_proc(self):
        p = self.mic_proc; self.mic_proc = None
        if p and p.returncode is None:
            try: p.terminate()
            except Exception: pass
            try: await asyncio.wait_for(p.wait(), timeout=2)
            except Exception:
                try: p.kill()
                except Exception: pass

    def _mic_spectrum(self, buf):
        """int16 PCM bytes -> list of 20 band heights (0-100).

        The mic has a high, steady self-noise floor, so a plain AGC lights every
        band constantly. Instead we track a per-band running noise floor (snaps
        down instantly, creeps up slowly) and show only energy *above* it, then
        AGC that residual. Result: bars rest near 0 and jump on real sound."""
        x = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
        n = len(x)
        if n < 8:
            return None
        x = x - x.mean()
        if self._mic_win is None or len(self._mic_win) != n:
            self._mic_win = np.hanning(n).astype(np.float32)
            freqs = np.fft.rfftfreq(n, 1.0 / MIC_RATE)
            edges = np.logspace(np.log10(MIC_FMIN), np.log10(MIC_FMAX), MIC_BANDS + 1)
            self._mic_edges = [np.searchsorted(freqs, e) for e in edges]
            self._mic_floor = None
        spec = np.abs(np.fft.rfft(x * self._mic_win))
        mags = np.empty(MIC_BANDS, dtype=np.float32)
        for i in range(MIC_BANDS):
            a, b = self._mic_edges[i], max(self._mic_edges[i] + 1, self._mic_edges[i + 1])
            seg = spec[a:b]
            mags[i] = np.sqrt(seg.mean()) if len(seg) else 0.0
        if self._mic_floor is None:
            self._mic_floor = mags.copy()
        self._mic_floor = np.where(mags < self._mic_floor, mags,
                                   self._mic_floor + (mags - self._mic_floor) * MIC_FLOOR_RISE)
        sig = np.clip(mags - self._mic_floor * MIC_NOISE_MARGIN, 0.0, None)
        self._mic_peak = max(self._mic_peak * MIC_PEAK_DECAY, float(sig.max()), MIC_MIN_PEAK)
        norm = np.clip(sig / (self._mic_peak + 1e-6), 0.0, 1.0) ** MIC_GAMMA * 100.0
        up = norm > self._mic_vals
        self._mic_vals = np.where(up,
                                  self._mic_vals + (norm - self._mic_vals) * MIC_ATTACK,
                                  self._mic_vals + (norm - self._mic_vals) * MIC_DECAY)
        vals = np.clip(self._mic_vals, 0, 100)
        # Sensitivity (we stream data directly, so the light's own byte is bypassed -
        # apply it here). 50 = neutral; >50 amplifies so quieter sound reacts (up to
        # 3x at 100); <50 raises a gate so only louder sound triggers.
        s = self.sound_sensitivity
        if s >= 50:
            vals = np.clip(vals * (1.0 + (s - 50) / 25.0), 0, 100)      # 1x .. 3x
        else:
            gate = (50 - s) / 50.0 * 80.0
            vals = np.clip((vals - gate) / max(1.0, 100.0 - gate) * 100.0, 0, 100)
        return vals.astype(np.uint8).tolist()

    async def _mic_loop(self):
        chunk = MIC_RATE // MIC_FPS
        nbytes = chunk * 2
        self._mic_peak = 1.0
        self._mic_vals[:] = 0
        try:
            while self.mic_enabled:
                if self.mic_proc is None or self.mic_proc.returncode is not None:
                    self.mic_proc = await asyncio.create_subprocess_exec(
                        "arecord", "-D", MIC_PCM, "-f", "S16_LE", "-c", "1",
                        "-r", str(MIC_RATE), "-t", "raw", "-",
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                    log.info("mic: arecord started on %s", MIC_PCM)
                try:
                    buf = await self.mic_proc.stdout.readexactly(nbytes)
                except asyncio.IncompleteReadError:
                    log.warning("mic: arecord stream ended, restarting")
                    await self._mic_stop_proc(); await asyncio.sleep(0.5); continue
                if not self.connected:
                    continue
                bands = self._mic_spectrum(buf)
                if bands is None:
                    continue
                if self._spectrum_mode():
                    payload = "0ae20a" + bytes(bands).hex()      # 20-band spectrum
                else:
                    payload = "0ae107" + f"{max(bands):02x}"      # single overall level
                await self.send(payload, pace=False)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.warning(f"mic loop error: {e}")
        finally:
            await self._mic_stop_proc()

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
        # Force BlueZ to drop any lingering link. Without this, a failed
        # write/keepalive ("Service Discovery has not been performed yet") leaves
        # BlueZ showing Connected=yes; the light then won't advertise, so our
        # rescan reports "not found" and we get stuck reconnecting to a stale link.
        try:
            p = await asyncio.create_subprocess_exec(
                "bluetoothctl", "disconnect", ADDR,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await asyncio.wait_for(p.wait(), timeout=5)
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

    async def set_link(self, enabled):
        """Toggle the BLE link. Off releases the light so a phone app can pair;
        On resumes the normal scan/connect/handshake loop (see connect_loop)."""
        enabled = bool(enabled)
        if enabled != self.link_enabled:
            self.link_enabled = enabled
            if enabled:
                log.info("BLE link ENABLED via switch - resuming connection")
            else:
                log.info("BLE link DISABLED via switch - releasing light for phone app")
                if self.mic_enabled:
                    await self.set_mic(False)
                await self._teardown()
        self._link_state()

    async def connect_loop(self):
        backoff = 3
        while True:
            if not self.link_enabled:
                if self.connected or self.client:
                    await self._teardown()
                await asyncio.sleep(1); continue
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
                    try: await self.client.start_notify(NOTIFY, self._on_notify)
                    except Exception: pass
                    await asyncio.sleep(0.3)
                    hs = self.proto.frame(HANDSHAKE)
                    await self.client.write_gatt_char(WRITE, hs, response=False); self._rec_write("W", hs); await asyncio.sleep(0.25)
                    qy = self.proto.frame(QUERY)
                    await self.client.write_gatt_char(WRITE, qy, response=False); self._rec_write("W", qy)
                    self.connected = True; backoff = 3
                    self._avail(True); log.info("CONNECTED to light"); await self._republish()
                    if self.mic_enabled:   # re-assert sound mode after a reconnect
                        await self._apply_sound()
                    if self.heart_enabled:  # re-enter draw mode after a reconnect
                        await self.send("0ae00e01")
                except Exception as e:
                    log.warning(f"connect failed: {e}")
                    await self._teardown()
                    await asyncio.sleep(backoff); backoff = min(backoff + 2, 15)
                    continue
            await asyncio.sleep(2)
            if self.connected:
                try:
                    ka = self.proto.frame(QUERY)
                    await self.client.write_gatt_char(WRITE, ka, response=False); self._rec_write("W", ka)
                except Exception as e:
                    log.warning(f"keepalive lost: {e}"); await self._teardown()

    async def worker(self):
        while True:
            cmd = await self.q.get()
            try: await self._apply(cmd)
            except Exception as e: log.warning(f"apply error: {e}")

    async def _apply(self, cmd):
        if "__record__" in cmd:
            self.start_record() if cmd["__record__"] else self.stop_record("manual"); return
        if "__link__" in cmd:
            await self.set_link(cmd["__link__"]); return
        if "__mic__" in cmd:
            await self.set_mic(cmd["__mic__"]); return
        if "__sound__" in cmd:
            await self.set_sound_style(cmd["__sound__"]); return
        if "__sensitivity__" in cmd:
            await self.set_sensitivity(cmd["__sensitivity__"]); return
        if "__cycle__" in cmd:
            await self.start_cycle(); return
        if "__heart__" in cmd:
            await self.set_heart(cmd["__heart__"]); return
        if "__smoke__" in cmd:
            await self.start_smoke(); return
        # any explicit light command exits sound-reactive / disco-heart mode first
        if any(k in cmd for k in ("effect", "color", "brightness", "state")):
            if self.mic_enabled: await self.set_mic(False)
            if self.heart_enabled: await self.set_heart(False)
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
    def _link_state(self):
        if self.mqtt: self.mqtt.publish(T_LINK_STATE, "ON" if self.link_enabled else "OFF", retain=True)
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
        link_sw = {
            "name": "Curtain BLE Link", "unique_id": f"{NODE}_ble_link",
            "command_topic": T_LINK_CMD, "state_topic": T_LINK_STATE,
            "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:bluetooth",
            "device": {"identifiers": [NODE], "name": "Curtain LED", "manufacturer": "Zengge/MagicHome2", "model": "BLE curtain"},
        }
        c.publish(DISCO_LINK, json.dumps(link_sw), retain=True)
        dev = {"identifiers": [NODE], "name": "Curtain LED", "manufacturer": "Zengge/MagicHome2", "model": "BLE curtain"}
        rec_sw = {
            "name": "Curtain Record BLE log", "unique_id": f"{NODE}_ble_record",
            "command_topic": T_REC_CMD, "state_topic": T_REC_STATE,
            "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:record-rec", "device": dev,
        }
        rec_file = {
            "name": "Curtain BLE log file", "unique_id": f"{NODE}_ble_record_file",
            "state_topic": T_REC_FILE, "icon": "mdi:file-document-outline", "device": dev,
        }
        mic_sw = {
            "name": "Curtain Sound Reactive (mic)", "unique_id": f"{NODE}_mic",
            "command_topic": T_MIC_CMD, "state_topic": T_MIC_STATE,
            "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:microphone", "device": dev,
        }
        sound_sel = {
            "name": "Curtain Sound Animation", "unique_id": f"{NODE}_sound",
            "command_topic": T_SOUND_CMD, "state_topic": T_SOUND_STATE,
            "options": SOUND_STYLES, "icon": "mdi:music", "device": dev,
        }
        sens_num = {
            "name": "Curtain Sound Sensitivity", "unique_id": f"{NODE}_sensitivity",
            "command_topic": T_SENS_CMD, "state_topic": T_SENS_STATE,
            "min": 0, "max": 100, "step": 1, "mode": "slider",
            "icon": "mdi:tune-vertical", "device": dev,
        }
        test_btn = {
            "name": "Test All Sound Styles (10s each)", "unique_id": f"{NODE}_soundtest",
            "command_topic": T_TEST_CMD, "payload_press": "PRESS",
            "icon": "mdi:play-box-multiple", "device": dev,
        }
        test_st = {
            "name": "Sound Test Status", "unique_id": f"{NODE}_soundtest_status",
            "state_topic": T_TEST_STATE, "icon": "mdi:information-outline", "device": dev,
        }
        c.publish(DISCO_REC, json.dumps(rec_sw), retain=True)
        c.publish(DISCO_REC_FILE, json.dumps(rec_file), retain=True)
        if MIC_PCM:
            c.publish(DISCO_MIC, json.dumps(mic_sw), retain=True)
            c.publish(DISCO_SOUND, json.dumps(sound_sel), retain=True)
            c.publish(DISCO_SENS, json.dumps(sens_num), retain=True)
            c.publish(DISCO_TEST, json.dumps(test_btn), retain=True)
            c.publish(DISCO_TESTST, json.dumps(test_st), retain=True)
        heart_sw = {
            "name": "Curtain Disco Heart", "unique_id": f"{NODE}_heart",
            "command_topic": T_HEART_CMD, "state_topic": T_HEART_STATE,
            "payload_on": "ON", "payload_off": "OFF", "icon": "mdi:heart-multiple", "device": dev,
        }
        c.publish(DISCO_HEART, json.dumps(heart_sw), retain=True)
        smoke_btn = {
            "name": "Curtain Smoke Test", "unique_id": f"{NODE}_smoke",
            "command_topic": T_SMOKE_CMD, "payload_press": "PRESS",
            "icon": "mdi:test-tube", "device": dev,
        }
        smoke_st = {
            "name": "Smoke Test Status", "unique_id": f"{NODE}_smoke_status",
            "state_topic": T_SMOKE_STATE, "icon": "mdi:clipboard-check-outline", "device": dev,
        }
        c.publish(DISCO_SMOKE, json.dumps(smoke_btn), retain=True)
        c.publish(DISCO_SMOKEST, json.dumps(smoke_st), retain=True)
        c.subscribe(T_CMD); c.subscribe(T_RAW); c.subscribe(T_LINK_CMD); c.subscribe(T_REC_CMD)
        c.subscribe(T_MIC_CMD); c.subscribe(T_SOUND_CMD); c.subscribe(T_SENS_CMD); c.subscribe(T_TEST_CMD)
        c.subscribe(T_HEART_CMD); c.subscribe(T_SMOKE_CMD)
        light._link_state(); light._rec_publish(); light._mic_state(); light._sound_state(); light._sensitivity_state(); light._cycle_state(); light._heart_state(); light._smoke_state()

    def on_message(c, u, msg):
        if msg.topic == T_REC_CMD:
            on = msg.payload.decode().strip().upper() in ("ON", "1", "TRUE")
            loop.call_soon_threadsafe(light.q.put_nowait, {"__record__": on}); return
        if msg.topic == T_LINK_CMD:
            on = msg.payload.decode().strip().upper() in ("ON", "1", "TRUE")
            loop.call_soon_threadsafe(light.q.put_nowait, {"__link__": on}); return
        if msg.topic == T_MIC_CMD:
            on = msg.payload.decode().strip().upper() in ("ON", "1", "TRUE")
            loop.call_soon_threadsafe(light.q.put_nowait, {"__mic__": on}); return
        if msg.topic == T_SOUND_CMD:
            loop.call_soon_threadsafe(light.q.put_nowait, {"__sound__": msg.payload.decode().strip()}); return
        if msg.topic == T_SENS_CMD:
            loop.call_soon_threadsafe(light.q.put_nowait, {"__sensitivity__": msg.payload.decode().strip()}); return
        if msg.topic == T_TEST_CMD:
            loop.call_soon_threadsafe(light.q.put_nowait, {"__cycle__": True}); return
        if msg.topic == T_HEART_CMD:
            on = msg.payload.decode().strip().upper() in ("ON", "1", "TRUE")
            loop.call_soon_threadsafe(light.q.put_nowait, {"__heart__": on}); return
        if msg.topic == T_SMOKE_CMD:
            loop.call_soon_threadsafe(light.q.put_nowait, {"__smoke__": True}); return
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
