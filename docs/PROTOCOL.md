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
