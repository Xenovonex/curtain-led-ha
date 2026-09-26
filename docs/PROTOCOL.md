# BLE protocol (Zengge / MagicHome2 curtain, `com.zennge.magichome2`)

Reverse-engineered from Android HCI snoop logs. Verified on a 20×20 (400-LED)
RGBIC curtain that advertises a name like `IOTBT####` and manufacturer ID
`0x5A02`.

## GATT

| Handle | UUID     | Role  |
|--------|----------|-------|
| write  | `0xFF01` | commands (write-without-response) |
| notify | `0xFF02` | responses (enable via its CCCD)   |

Connect, enable notifications on `0xFF02`, then write commands to `0xFF01`.

## Transport frame

Every write to `0xFF01` is wrapped:

```
byte0      0x01              (0x05 on notifications from the device)
byte1      seq               increments per message, reset on connect
byte2..3   0x80 0x00         constant
byte4      0x00              constant
byte5      payloadLen - 1
byte6..7   payloadLen        (16-bit, big-endian)
byte8..    payload           (starts 0x0a for requests, 0x15 in notifications)
```

Total length = `8 + payloadLen`.

## Session

1. Enable notifications (write `0x0001` to the CCCD of `0xFF02`).
2. Handshake payload: `0a 10 14 1a 09 18 0f 2a 1b 04 00 0f c6` (last byte = checksum).
3. Query payload: `0a ea 81 8a 8b 59` → device replies on `0xFF02` with state.
4. Send commands. A periodic query doubles as a keepalive.

## Command payloads (after the `0x0a` opcode)

| Function | Payload | Notes |
|----------|---------|-------|
| Power toggle | `0a 71 24` / `0a 71 23` | acts as a **toggle** in practice |
| Solid color | `0a e2 0b <hue> <Y>` | `hue`: 0=red, ~0x1e=yellow, ~0x3c=green, ~0x78 blue... `Y`≥0x80 = brightness of the hue; `Y`<0x80 = white brightness |
| Brightness | `0a e0 02 00 02 <v> 50` | `v` 0x00–0x64 |
| Built-in animation | `0a e0 02 00 <id> <speed> <bright>` | `id` 1–17 (rainbow wave, purple wave, fades, …), `speed`/`bright` 0x00–0x64 |
| Hearts animation | `0a e2 05 02 00×9 1e <speed> <C> <dir>` | `dir`: 00 in-place, 02 ←, 03 →, 04 ↑, 05 ↓; `C` = brightness |
| Sound style / palette | `0a e1 05 00 50 <style> 00 00 <sens> …pad… a1 00 00 00 <n> [a1 <hue> 64 64]×n` | selects a sound-reactive animation. `style` byte: only `1,2,3,4,7,8,12,13` render; `sens` (byte 9, 0x00–0x64) = the app's "sensitivity" |
| Sound spectrum frame | `0a e2 0a <b0..b19>` | 20 spectrum-band heights (`0x00`–`0x64`) — drives sound styles **1–4**. **Streamed** ~8–10×/s |
| Sound level frame | `0a e1 07 <level>` | single overall level (`0x00`–`0x64`) — drives sound styles **5+** (e.g. 7,8,12,13). **Streamed** ~8–10×/s |

### Pixel drawing (the important one)

```
0a e0 0e 01                                enter draw mode
0a e2 06 <hue> <sat> <val> <bitmap>        draw all set bits in that color
```

- `bitmap` = `ceil(W*H/8)` bytes; **50 bytes for 400 LEDs**.
- Bit index → pixel; on a 20×20 curtain the panel is **column-major**: to light
  logical (row, col) set bit `col*20 + row` (MSB-first: bit 0 = the byte's high bit).
- `sat=0x64 val=0x64` = full color; `sat=0x00` = white (use `val` for brightness);
  `sat=val=0x00` = black/off.
- Multi-color image = one `e2 06` per color. Clear = one `e2 06` with black over
  every bit. Commit isn't required for a live display.
- `hue` here ≈ HSV degrees ÷ 2 (red=0x00, green=0x3c, blue=0x78).

The vendor app also has an `ea 01/02/03/04` bulk (RLE) upload used when *saving*
a pattern; the simpler `e2 06` bitmap above is sufficient for live drawing and
is what this project uses.

## Notifications

Responses arrive on `0xFF02` with the same header (byte0 `0x05`) and a payload
starting `0x15`, echoing the command and current state (mode, brightness, color).

## Sound / microphone mode (decoded)

The vendor "DJ" / sound mode is **not a single command — it is a live stream**.
Decoded from an Android HCI snoop of the app (`btsnoop_hci.log`) and confirmed on
hardware (2026-09-25):

- App writes go to handle `0xFF01`; the `15 e1 06 …` / `16 ea 81 …` frames seen on
  `0xFF02` are the light's **notification replies**, not commands — don't confuse
  the two when reading a snoop.
- The animation is chosen with an `0a e1 05 …` frame (`style` byte + palette +
  `sens` sensitivity byte). Only style bytes **1,2,3,4,7,8,12,13** render; the app
  never uses the others.
- The reactivity is then fed **from the phone mic** as a live stream (~8–10 fps),
  and the *stream format depends on the style*:
  - **Styles 1–4** → `0a e2 0a` + **20 bytes** (a 20-band FFT spectrum → "bars").
  - **Styles 5+** → `0a e1 07 <level>` (a single overall level → "glitter"/level).
- `sens` (byte 9 of `e1 05`) is the app's sensitivity slider. When you stream the
  data yourself it has little effect (the light just shows what you send), so this
  project applies sensitivity in its own DSP instead.
- `0a e2 31` is a generic init/status query (it appears once in *every* session,
  sound or not) — it is **not** a sound-mode enable. The light has no standalone
  "use its own built-in mic" mode reachable by a single command.
- Sending a single `e2 0a`/`e1 07` frame just **freezes** the panel on that frame.
  Replaying the captured stream at its original timing reproduces the exact bars
  from the sounds made during capture — proving it must be streamed live.

### Reproducing it: `CURTAIN_MIC_PCM` (local mic → `e2 0a` stream)

Because sound mode is a stream, the bridge generates it from a local capture
device rather than replaying a fixed payload. Set `CURTAIN_MIC_PCM` to an ALSA
capture device; the bridge then exposes (via MQTT discovery):

- **Curtain Sound Reactive (mic)** switch — turns the mode on/off.
- **Curtain Sound Animation** select — the working styles (1,2,3,4,7,8,12,13). The
  bridge sends the matching `e1 05` frame and streams the right format per style:
  `e2 0a` (spectrum) for 1–4, `e1 07` (level) for the rest.
- **Curtain Sound Sensitivity** number (0–100) — applied in the bridge's DSP
  (>50 amplifies so quieter sound reacts, <50 raises a threshold).
- **Test All Sound Styles** button — steps bytes 1–15 (10 s each) to re-verify.

While on it captures audio, computes a 20-band log-spaced FFT (per-band running
noise-floor subtraction + AGC + attack/decay smoothing), and streams at ~8 fps
(one read == one ALSA period so delivery is smooth). Turning it off re-asserts a
normal effect so the panel isn't left on the last frame. Keep any such stream
light — large/fast frames saturate this light's BLE link and drop the connection.

### Disco heart + smoke test

The bridge also exposes a **Curtain Disco Heart** switch (pixel-draws a heart via
`e2 06` and re-draws it ~1.5 fps with a rotating hue for a colour-cycling heart)
and a **Curtain Smoke Test** button (runs colours → animation → disco heart →
sound bars → scroll through the whole feature set for a quick camera check).

If the mic is shared with another consumer (e.g. a Wyoming voice satellite that
holds the USB mic), point `CURTAIN_MIC_PCM` at an ALSA **dsnoop** PCM so both can
read it. dsnoop runs at the mic's native rate; set `CURTAIN_MIC_RATE` to match
(e.g. `48000`) and let each client `plug` down. Example `~/.asoundrc`:

```
pcm.dsnoop_mic {
    type dsnoop
    ipc_key 2048
    ipc_key_add_uid true
    slave { pcm "hw:2,0"; channels 1; rate 48000; format S16_LE
            period_size 6000; buffer_size 24000 }
}
pcm.mic_shared { type plug; slave.pcm "dsnoop_mic" }
```

### Capturing more app behaviour (recorder / phone snoop)

The dashboard's **Record BLE log** switch logs every frame the bridge writes and
every notification, to `CURTAIN_LOG_DIR` (auto-stops after `CURTAIN_RECORD_MINUTES`);
decode with `python bridge/decode_ble_log.py`. Because these lights are
single-central, the bridge can't capture the phone app while connected — for that,
release the **Kiosk BLE link** and take an Android HCI snoop
(`FS/data/log/bt/btsnoop_hci.log`), then strip the 8-byte transport header
(`01 <seq> 80 00 00 <len-1> 00 <len>`) from writes to `0xFF01`.
