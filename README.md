# Curtain LED → Home Assistant

Local, app-free control of a **Zengge / "MagicHome2"** BLE LED curtain in
**Home Assistant** — including a live **20×20 pixel-drawing studio**.

These cheap RGBIC "DIY light show" curtains are sold under many names (SurpLife,
YIQU, Lichaser, …) and use the Android/iOS app whose package is
`com.zennge.magichome2`. They are **BLE-only** and have no official Home
Assistant integration. This project reverse-engineers the BLE protocol (see
[`docs/PROTOCOL.md`](docs/PROTOCOL.md)) and bridges the light to HA over MQTT.

> Works with a 20×20 (400-LED) curtain. Other sizes: set `GRID_W`/`GRID_H`.

## What you get

- A normal Home Assistant **light** entity (on/off, brightness, RGB, effects).
- A **Light Lab** dashboard: hue/brightness/white sliders, 17 named built-in
  animations, and a heart-animation direction pad.
- A **Draw** dashboard: a touch **paint grid**, a **34-pattern library**
  (flags, game sprites, holiday/Halloween, shapes) with recoloring, move/scroll/
  fade animations, and savable multi-frame **sequences** with transitions.

## Architecture

```
  Home Assistant  ──MQTT──►  bridge (near the light, has Bluetooth)  ──BLE──►  curtain
        ▲                          ▲
        └── Draw Studio web app ───┘  (publishes pixel frames to the same MQTT topic)
```

- **`bridge/`** — `curtain_bridge.py`: holds one BLE connection to the light and
  bridges MQTT↔BLE. Run it on a machine with Bluetooth **physically near the
  light** (a Raspberry Pi works well). Publishes MQTT discovery so HA auto-adds
  the light.
- **`draw-studio/`** — `draw_server.py` + `index.html`: a small web app that
  turns a 20×20 grid into per-pixel BLE draw commands and publishes them to the
  bridge's MQTT `raw` topic (rate-limited so animations can't flood BLE).
- **`homeassistant/`** — a config **package** (helper sliders + automations) and
  two **dashboards** you paste into HA.

## Requirements

- An MQTT broker HA can reach (e.g. the Mosquitto add-on).
- A Bluetooth-capable host near the light (Linux + BlueZ; Raspberry Pi ideal).
- Python 3.9+.

## Setup

### 1. Find your light's BLE address

With the light powered on and no app connected, scan for it:

```bash
bluetoothctl --timeout 15 scan on | grep -i IOTBT   # name is often IOTBT####
```

Or use any BLE scanner. Note the address (`AA:BB:CC:DD:EE:FF`) — it advertises
manufacturer ID `0x5A02`.

### 2. Configure

Copy `.env.example` to `/etc/curtain-led.env` and fill in your light address and
MQTT broker details. `CURTAIN_NODE` must match between the bridge, the draw
server, and the HA package/dashboards (default `curtain`).

### 3. Run the bridge (on the Bluetooth host near the light)

```bash
sudo mkdir -p /opt/curtain-led && sudo chown $USER /opt/curtain-led
cp -r bridge /opt/curtain-led/
cd /opt/curtain-led/bridge
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
# quick test:
set -a; . /etc/curtain-led.env; set +a; ./venv/bin/python curtain_bridge.py
# then install as a service:
sudo cp curtain-bridge.service /etc/systemd/system/
sudo systemctl enable --now curtain-bridge
```

Home Assistant should now show a **Curtain Lights** entity (MQTT discovery).

### 4. Run the draw studio (any host that can reach MQTT)

```bash
cp -r draw-studio /opt/curtain-led/
cd /opt/curtain-led/draw-studio
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
sudo cp curtain-draw.service /etc/systemd/system/
sudo systemctl enable --now curtain-draw       # serves on :8095
```

### 5. Add the Home Assistant pieces

- Put `homeassistant/packages/curtain_led.yaml` in your `config/packages/`
  folder (enable packages with `homeassistant: packages: !include_dir_named packages`),
  then restart HA. This adds the slider helpers + automations.
- Add each dashboard (Settings → Dashboards → New dashboard → ⋮ → Raw
  configuration editor → paste):
  - `homeassistant/dashboards/light-lab.yaml`
  - `homeassistant/dashboards/draw.yaml` (edit `YOUR_SERVER_HOST` to the host
    running the draw server).

## Notes & limits

- **Bluetooth bandwidth**: every full-grid repaint is ~10 BLE writes ≈ 0.5–1 s,
  so animations run at roughly **1–2 fps** — smooth slideshow, not fluid video.
- One BLE central at a time — keep the vendor app closed while the bridge runs.
- Turning the light fully off drops the BLE link; it reconnects when powered on
  again (may take ~30–60 s, or power-cycle if it stops advertising).

## Disclaimer

Reverse-engineered and provided as-is, unaffiliated with any manufacturer. Use
at your own risk. Protocol details in [`docs/PROTOCOL.md`](docs/PROTOCOL.md).
