# QEMU HiSilicon Fastboot Protocol Specification

This document specifies the HiSilicon boot ROM serial protocol as implemented
in real hardware, intended as a reference for implementing emulation in QEMU.

Three protocol variants exist, each used by different SoC families.

## Common Parameters

- **UART**: 115200 baud, 8 data bits, no parity, 1 stop bit (8N1)
- **CRC**: CRC-16/CCITT, polynomial 0x1021, finalized with 2 zero-byte padding
- **Max payload**: 1024 bytes per DATA frame
- **ACK byte**: 0xAA

### CRC-16/CCITT Algorithm

```python
def calc_crc(data: bytes, crc: int = 0) -> int:
    for byte in data:
        crc = ((crc << 8) | byte) ^ CRC_TABLE[(crc >> 8) & 0xFF]
    for _ in range(2):
        crc = ((crc << 8) | 0) ^ CRC_TABLE[(crc >> 8) & 0xFF]
    return crc & 0xFFFF
```

CRC_TABLE is a standard 256-entry CCITT lookup table (see `src/defib/protocol/crc.py`).

---

## Protocol 1: Standard (Classic HiSilicon)

**SoCs**: Hi3516CV300, Hi3516EV200, Hi3518EV200, Hi3520D, and ~90 others.

### Phase 1: Boot ROM Handshake

1. **Device** continuously sends `0x20` bytes on UART after power-on/reset
2. **Host** monitors for 5 consecutive `0x20` bytes (ignoring `0x00` bytes)
3. **Host** sends `0xAA` (ACK)
4. **Device** enters boot download mode and stops sending `0x20`

### Phase 2: DDR Initialization Step

Transfer 64 bytes of ARM machine code to on-chip SRAM:

1. **HEAD frame** (14 bytes):
   ```
   Offset  Size  Value
   0       4     FE 00 FF 01 (magic)
   4       4     Length (big-endian, = 0x00000040 for 64 bytes)
   8       4     Address (big-endian, e.g. 0x04013000)
   12      2     CRC-16 of bytes 0-11 (big-endian)
   ```
   Host sends, retries up to 16 times with 30ms timeout per try.
   Device responds with `0xAA` on success.

2. **DATA frame** (variable):
   ```
   Offset  Size  Value
   0       1     DA (magic)
   1       1     Sequence number (starting at 1)
   2       1     ~Sequence (bitwise complement)
   3       N     Payload (64 bytes for DDR step)
   3+N     2     CRC-16 of bytes 0 through 2+N (big-endian)
   ```
   Host sends, retries up to 32 times with 150ms timeout.
   Device responds with `0xAA`.

3. **TAIL frame** (5 bytes):
   ```
   Offset  Size  Value
   0       1     ED (magic)
   1       1     Next sequence number (= 2 for DDR step)
   2       1     ~Sequence
   3       2     CRC-16 of bytes 0-2 (big-endian)
   ```
   Device responds with `0xAA`.

### Phase 3: SPL Transfer

Same HEAD/DATA/TAIL structure as DDR step, but:
- Length: from profile `FILELEN[1]` (e.g., 0x4F00 = 20224 bytes)
- Address: from profile `ADDRESS[1]` (e.g., 0x04010500, SRAM)
- Data: first `FILELEN[1]` bytes of the firmware file
- Split into 1024-byte chunks, each in its own DATA frame

### Phase 4: U-Boot Transfer

Same structure:
- Length: full firmware file size
- Address: from profile `ADDRESS[2]` (e.g., 0x81000000, DDR)
- Data: entire firmware file, split into 1024-byte chunks

### QEMU Implementation Notes

The emulator should:
1. Send `0x20` bytes at ~1 byte per 87μs (115200 baud)
2. Watch for `0xAA` byte to transition to download mode
3. For each received frame: validate magic, verify CRC, respond `0xAA`
4. Store received data at the specified addresses in emulated memory
5. After TAIL frame for U-Boot, jump to the U-Boot entry point

---

## Protocol 2: V500 (GK7205V500 Series)

**SoCs**: GK7205V500, GK7205V510, GK7205V530, XM7205V500/510/530.

### Phase 1: Handshake

1. **Host** repeatedly sends a 14-byte handshake frame:
   ```
   Offset  Size  Value
   0       4     BD 00 FF 01 (magic)
   4       8     00 00 00 00 00 00 00 00 (zeros)
   12      2     CRC-16 of bytes 0-11 (big-endian)
   ```

2. **Device** responds with 14 bytes:
   ```
   Offset  Size  Value
   0       2     BD 00 (magic)
   2       6     (varies)
   8       4     Chip ID (big-endian, e.g. 0x7205V500)
   12      2     (varies)
   ```

3. Host detects response starting with `BD 00` within 20-second timeout.

### Phase 2: Multi-Area Boot Transfer

Three areas are sent sequentially:

**Area 1 - HEAD (8KB)**:
- Address: 0x00000000
- Data: firmware bytes 0-8191

**Area 2 - AUX**:
- Address: 0x00002000 (= 8192)
- Size: read from firmware offset 1024 as uint32 LE
- Data: firmware bytes starting at offset 8192

**100ms pause between AUX and BOOT areas**

**Area 3 - BOOT**:
- Address: 0x41000000 (DDR)
- Data: entire firmware file

### Data Transfer Protocol

Each area uses HEAD/DATA/TAIL frames identical to the Standard protocol,
but with per-chunk acknowledgment:

- After each DATA frame, host waits up to 4 seconds for response
- `0xAA` = ACK, proceed to next chunk
- `U` (0x55) = NAK, retransmit same chunk (up to 10 retries)

### QEMU Implementation Notes

1. Respond to `BD 00 FF 01` handshake with chip ID response
2. Accept HEAD/DATA/TAIL frames with per-chunk ACK
3. Handle NAK retransmission (respond `U` to test retry logic)
4. Load received data into emulated memory at specified addresses

---

## Protocol 3: CV6xx (HI3516CV6xx Series)

**SoCs**: HI3516CV608, HI3516CV610, HI3516CV613, HI3516DV500, HI3519DV500.

### Phase 1: Handshake

1. **Host** repeatedly sends a handshake frame:
   ```
   Offset  Size  Value
   0       8     EF BE AD DE 12 00 F0 0F (magic - note: DEADBEEF reversed)
   8       4     Baud rate (little-endian, 0x0001C200 = 115200)
   12      4     08 01 00 09 (serial format params)
   16      2     CRC-16 of bytes 0-15 (LITTLE-ENDIAN, unlike other protocols)
   ```
   Sent every 10ms.

2. **Device** responds with ASCII text containing either:
   - `uart ddr` — DDR needs initialization
   - `uart flash` — flash recovery mode

3. Host detects the marker string, waits 500ms for settle, then clears input buffer.

### Phase 2: Board ID Query

1. **Host** sends a board ID query frame:
   ```
   Offset  Size  Value
   0       4     CE 00 FF 01 (magic)
   4       4     Current timestamp (big-endian uint32)
   8       4     Current timestamp (big-endian uint32, repeated)
   12      2     CRC-16 of bytes 0-11 (big-endian)
   ```

2. **Device** responds with 11 bytes at `0xCE` marker:
   ```
   Offset  Size  Value
   0       1     CE (marker)
   1       1     CPU ID
   2       2     (padding)
   4       4     Board ID (big-endian uint32)
   8       2     (padding)
   10      1     AA (ACK)
   ```

3. Host scans for `0xCE` marker within 2-second timeout.
   If no response, defaults to board_id=0.

### Phase 3: Composite Boot File Structure

The firmware file contains three concatenated sections:

**GSL (Generic SoC Library)**:
- Magic: `0x4BB4D22D` at file offset 2048 (little-endian uint32)
- Length: at file offset 2084 (little-endian uint32)
- Total size: length + 3072 bytes
- Data: file bytes 0 through gsl_size-1

**DDR Parameters**:
- Magic: `0x4B87A52D` at offset (gsl_size + 1024)
- Table offset: at magic+32 (uint32 LE)
- Table size: at magic+36 (uint32 LE)
- Table count: at magic+40 (uint32 LE)
- Board mapping: 8 bytes at magic+300 (maps board_id → table_index)
- DDR table data: header (2048 bytes from gsl_size) + selected table

**U-Boot**:
- Magic: `0x4BF01E2D` (searched after DDR params section)
- Length: at magic+36 (uint32 LE)
- Total size: length + 1024 bytes

### Phase 4: Transfer Sequence

All data transfers use the V500-style HEAD/DATA/TAIL with per-chunk ACK.

1. **Send GSL** to address `0x04020000`
2. **Query Board ID** (Phase 2 above)
3. **Build DDR table**: select table using board_mapping[board_id]
4. **Send DDR table** to address `0x41000000`
5. **Wait 1.5 seconds** for DDR training
   - Device may output ASCII text during this time (training status)
   - Host should read and log/display this output
6. **Send U-Boot** to address `0x41000000`

### QEMU Implementation Notes

1. Respond to `DEADBEEF` handshake with `uart ddr\n`
2. Respond to `CE` board ID query with appropriate CPU ID and board ID
3. Accept GSL transfer at 0x04020000 — this is the initial boot code
4. Accept DDR table at 0x41000000 — use this to configure emulated DDR
5. Output DDR training status text (e.g., "DDR training OK\n")
6. Accept U-Boot transfer at 0x41000000
7. After final TAIL frame, execute U-Boot from 0x41000000

### Magic Numbers Summary

| Magic | Location | Purpose |
|-------|----------|---------|
| `0x4BB4D22D` | File offset 2048 | GSL section marker |
| `0x4B87A52D` | After GSL + 1024 | DDR params section marker |
| `0x4BF01E2D` | After DDR params | U-Boot section marker |
| `0xDEADBEEF` | Handshake frame (reversed as EF BE AD DE) | CV6xx handshake magic |

---

## Frame Reference Summary

| Frame | Magic | Size | CRC Position | CRC Endian |
|-------|-------|------|-------------|------------|
| HEAD | FE 00 FF 01 | 14B | bytes 12-13 | Big |
| DATA | DA | 6+N B | last 2 bytes | Big |
| TAIL | ED | 5B | bytes 3-4 | Big |
| V500 Handshake | BD 00 FF 01 | 14B | bytes 12-13 | Big |
| CV6xx Handshake | EF BE AD DE ... | 18B | bytes 16-17 | **Little** |
| CV6xx Board ID | CE 00 FF 01 | 14B | bytes 12-13 | Big |

---

## Testing Against Real Hardware

Use `defib capture` to record real UART sessions:

```bash
defib capture -p /dev/ttyUSB0 -o session.dcap
```

The `.dcap` file contains timestamped bidirectional traffic that can be
replayed against the QEMU emulator to verify protocol compliance.
