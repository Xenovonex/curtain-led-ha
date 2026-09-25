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
| Effect palette | `0a e1 05 00 <speed> 01 <dir> <n> 64 …pad… a1 00 00 00 <n> [a1 <hue> 64 64]×n` | color-cycle with a palette |

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

## Sound / microphone mode (not yet decoded)

The light has a built-in microphone "DJ" mode that animates colour/brightness by
how loud the room is. Its BLE command is **not captured yet**. Analysis of the
available Android HCI snoop logs (`btsnoop_hci.log` inside the vendor-app bug
reports) showed only colour tests and pixel-drawing sessions on the plaintext
write handle (`0xFF01`) — no mic command. Note the vendor app also opens an
**encrypted channel** (a separate GATT handle carrying `a9fe…` blocks); if the
mic toggle is sent there rather than on `0xFF01`, it can't be read from a snoop
without the session keys — so prefer confirming it appears on `0xFF01`.

### Capturing it — option A: this bridge's recorder (no phone logs)

The dashboard has a **Record BLE log** switch. It logs every frame the bridge
writes **and** every notification the light sends, to a text file in
`CURTAIN_LOG_DIR` (default `~/curtain-ble-logs`), and auto-stops after 20 min
(`CURTAIN_RECORD_MINUTES`).

1. Keep **Kiosk BLE link** ON so the bridge stays connected.
2. Turn **Record BLE log** on.
3. Run the audio test. Driving effects from Home Assistant is captured fully.
   Opening the vendor app *may* also be captured — but only if the light accepts
   a second BLE connection while the bridge holds one; most of these allow only
   one central, in which case the app can't connect and you need option B.
4. Turn recording off (or let it auto-stop) and decode:
   `python bridge/decode_ble_log.py ~/curtain-ble-logs/ble-*.log`.
   Lines flagged `??? UNKNOWN` are candidate new commands.

### Capturing it — option B: phone HCI snoop (sees the app for sure)

1. Turn the **Kiosk BLE link** switch **off** so the bridge releases the light.
2. On Android, enable *Developer options → Bluetooth HCI snoop log* (toggle
   Bluetooth off/on so logging starts fresh).
3. Open the vendor app, connect, and turn the microphone / music mode on and off
   a couple of times. Keep it brief so the command is easy to find.
4. Pull the log (`adb bugreport` or Developer options → *Take bug report*) and
   find `FS/data/log/bt/btsnoop_hci.log`.
5. Decode: look for **write commands** (ATT opcode `0x52`) to handle `0xFF01`
   whose value starts `01 <seq> 80 00 00 <len-1> 00 <len>`; strip that 8-byte
   header. Any opcode outside the known set (`71` power, `e0 02` animation,
   `e2 0b` colour, `e2 06` draw, `ea …` bulk upload) is a candidate.

For a true passive capture of the phone↔light link without either compromise, a
dedicated BLE sniffer (e.g. nRF52840 + nRF Sniffer, or Ubertooth) is required —
a normal host adapter can't see another device's connection.

### Plugging in the result

Put the decoded payload hex (without the transport header — the bridge adds it)
into **Mic ON payload / Mic OFF payload** on the dashboard, or `curtain/raw`.

Until then, the dashboard's **Mic mode** toggle and payload boxes are wired up
but send nothing (the automation no-ops on an empty payload), so the control is
harmless. The **Raw command (hex)** box publishes any payload to `curtain/raw`
for live testing while you decode.
