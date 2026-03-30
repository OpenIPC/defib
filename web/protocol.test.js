/**
 * Tests for defib JS protocol code.
 * Cross-validated against Python implementation output.
 *
 * Run: node --test web/protocol.test.js
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const {
  CRC_TABLE, calcCrc, appendCrc, appendCrcLE, verifyCrc,
  buildHeadFrame, buildDataFrame, buildTailFrame, chunkData,
  parseCv6xxBoot, V500_SOCS, CV6XX_SOCS,
} = require('./protocol.js');

// Helper: hex string → Uint8Array
function hex(s) {
  const bytes = [];
  for (let i = 0; i < s.length; i += 2) bytes.push(parseInt(s.substring(i, i+2), 16));
  return new Uint8Array(bytes);
}

// Helper: Uint8Array → hex string
function toHex(arr) {
  return Array.from(arr).map(b => b.toString(16).padStart(2, '0')).join('');
}

// ================================================================
// CRC-16 Tests — cross-validated against Python calc_crc()
// ================================================================
describe('CRC-16/CCITT', () => {
  it('CRC table has 256 entries', () => {
    assert.equal(CRC_TABLE.length, 256);
  });

  it('CRC table first entry is 0', () => {
    assert.equal(CRC_TABLE[0], 0x0000);
  });

  it('CRC table second entry is 0x1021 (polynomial)', () => {
    assert.equal(CRC_TABLE[1], 0x1021);
  });

  // Cross-validated test vectors from Python
  it('empty data → 0x0000', () => {
    assert.equal(calcCrc(new Uint8Array([])), 0x0000);
  });

  it('0xAA → 0x14a0', () => {
    assert.equal(calcCrc(hex('aa')), 0x14a0);
  });

  it('HEAD magic FE 00 FF 01 → 0x2ec9', () => {
    assert.equal(calcCrc(hex('fe00ff01')), 0x2ec9);
  });

  it('HEAD frame payload (DDR step) → 0x519c', () => {
    assert.equal(calcCrc(hex('fe00ff010000004004013000')), 0x519c);
  });

  it('bytes 0-255 → 0x7e55', () => {
    const data = new Uint8Array(256);
    for (let i = 0; i < 256; i++) data[i] = i;
    assert.equal(calcCrc(data), 0x7e55);
  });

  it('DATA frame prefix + 100 bytes → 0xe305', () => {
    const data = new Uint8Array(103);
    data[0] = 0xda; data[1] = 0x01; data[2] = 0xfe;
    for (let i = 0; i < 100; i++) data[3+i] = i;
    assert.equal(calcCrc(data), 0xe305);
  });

  it('is deterministic', () => {
    const data = hex('deadbeef');
    assert.equal(calcCrc(data), calcCrc(data));
  });

  it('different data produces different CRC', () => {
    assert.notEqual(calcCrc(hex('010203')), calcCrc(hex('040506')));
  });

  it('result is always 16-bit', () => {
    for (const input of [hex(''), hex('ff'), hex('00'.repeat(1024))]) {
      const crc = calcCrc(input);
      assert.ok(crc >= 0 && crc <= 0xffff, `CRC ${crc} out of range`);
    }
  });
});

describe('appendCrc', () => {
  it('appends 2 bytes', () => {
    const result = appendCrc(hex('fe00ff01'));
    assert.equal(result.length, 6); // 4 + 2
  });

  it('preserves original data', () => {
    const result = appendCrc(hex('fe00ff01'));
    assert.equal(toHex(result.slice(0, 4)), 'fe00ff01');
  });

  it('big-endian CRC bytes match Python', () => {
    const result = appendCrc(hex('fe00ff01'));
    assert.equal(toHex(result.slice(4)), '2ec9');
  });

  it('result passes verifyCrc', () => {
    const result = appendCrc(hex('fe00ff010000004004013000'));
    assert.ok(verifyCrc(result));
  });
});

describe('appendCrcLE', () => {
  it('little-endian byte order', () => {
    const data = hex('010203');
    const be = appendCrc(data);
    const le = appendCrcLE(data);
    // LE should have the bytes swapped relative to BE
    assert.equal(le[le.length - 1], be[be.length - 2]);
    assert.equal(le[le.length - 2], be[be.length - 1]);
  });
});

describe('verifyCrc', () => {
  it('valid frame passes', () => {
    assert.ok(verifyCrc(appendCrc(hex('fe00ff01'))));
  });

  it('corrupted frame fails', () => {
    const frame = appendCrc(hex('fe00ff01'));
    frame[frame.length - 1] ^= 0xff;
    assert.ok(!verifyCrc(frame));
  });

  it('too short returns false', () => {
    assert.ok(!verifyCrc(new Uint8Array([0x00, 0x01])));
    assert.ok(!verifyCrc(new Uint8Array([0x00])));
    assert.ok(!verifyCrc(new Uint8Array([])));
  });

  it('roundtrip for various lengths', () => {
    for (const len of [1, 10, 100, 512, 1024]) {
      const data = new Uint8Array(len);
      for (let i = 0; i < len; i++) data[i] = i % 256;
      assert.ok(verifyCrc(appendCrc(data)), `Failed for length ${len}`);
    }
  });
});

// ================================================================
// Frame Tests — cross-validated against Python frame encoders
// ================================================================
describe('buildHeadFrame', () => {
  it('produces 14 bytes', () => {
    assert.equal(buildHeadFrame(0x40, 0x04013000).length, 14);
  });

  it('starts with FE 00 FF 01', () => {
    const frame = buildHeadFrame(0x40, 0x04013000);
    assert.equal(toHex(frame.slice(0, 4)), 'fe00ff01');
  });

  it('matches Python: length=0x40, addr=0x04013000', () => {
    assert.equal(toHex(buildHeadFrame(0x40, 0x04013000)), 'fe00ff010000004004013000519c');
  });

  it('matches Python: length=0x4F00, addr=0x04010500', () => {
    assert.equal(toHex(buildHeadFrame(0x4F00, 0x04010500)), 'fe00ff0100004f00040105001587');
  });

  it('big-endian length encoding', () => {
    const frame = buildHeadFrame(0x4F00, 0x04010500);
    assert.equal(toHex(frame.slice(4, 8)), '00004f00');
  });

  it('big-endian address encoding', () => {
    const frame = buildHeadFrame(0x40, 0x81000000);
    assert.equal(toHex(frame.slice(8, 12)), '81000000');
  });

  it('has valid CRC', () => {
    assert.ok(verifyCrc(buildHeadFrame(0x40, 0x04013000)));
  });
});

describe('buildDataFrame', () => {
  it('starts with DA', () => {
    const frame = buildDataFrame(1, new Uint8Array([0x00]));
    assert.equal(frame[0], 0xda);
  });

  it('seq and ~seq bytes', () => {
    const frame = buildDataFrame(5, new Uint8Array([0x00]));
    assert.equal(frame[1], 5);
    assert.equal(frame[2], (~5) & 0xff);
  });

  it('seq=0x42 → complement 0xBD', () => {
    const frame = buildDataFrame(0x42, new Uint8Array([0x00]));
    assert.equal(frame[1], 0x42);
    assert.equal(frame[2], 0xbd);
  });

  it('matches Python: seq=1, 64-byte payload', () => {
    const payload = new Uint8Array(64);
    for (let i = 0; i < 64; i++) payload[i] = i;
    const expected = 'da01fe000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f419d';
    assert.equal(toHex(buildDataFrame(1, payload)), expected);
  });

  it('has valid CRC', () => {
    const payload = new Uint8Array(100);
    assert.ok(verifyCrc(buildDataFrame(1, payload)));
  });

  it('1024-byte max payload', () => {
    const payload = new Uint8Array(1024);
    const frame = buildDataFrame(1, payload);
    assert.ok(verifyCrc(frame));
    assert.equal(frame.length, 3 + 1024 + 2);
  });
});

describe('buildTailFrame', () => {
  it('produces 5 bytes', () => {
    assert.equal(buildTailFrame(2).length, 5);
  });

  it('starts with ED', () => {
    assert.equal(buildTailFrame(2)[0], 0xed);
  });

  it('matches Python: seq=2', () => {
    assert.equal(toHex(buildTailFrame(2)), 'ed02fdbab0');
  });

  it('matches Python: seq=25', () => {
    assert.equal(toHex(buildTailFrame(25)), 'ed19e6c663');
  });

  it('has valid CRC', () => {
    assert.ok(verifyCrc(buildTailFrame(10)));
  });
});

// ================================================================
// chunkData Tests
// ================================================================
describe('chunkData', () => {
  it('exact chunk size', () => {
    const data = new Uint8Array(1024);
    const chunks = chunkData(data, 1024);
    assert.equal(chunks.length, 1);
    assert.equal(chunks[0].length, 1024);
  });

  it('multiple chunks', () => {
    const data = new Uint8Array(2500);
    const chunks = chunkData(data, 1024);
    assert.equal(chunks.length, 3);
    assert.equal(chunks[0].length, 1024);
    assert.equal(chunks[1].length, 1024);
    assert.equal(chunks[2].length, 452);
  });

  it('preserves data', () => {
    const data = new Uint8Array([1, 2, 3, 4, 5]);
    const chunks = chunkData(data, 2);
    const reassembled = new Uint8Array([...chunks[0], ...chunks[1], ...chunks[2]]);
    assert.deepEqual(reassembled, data);
  });

  it('empty data', () => {
    assert.equal(chunkData(new Uint8Array([]), 1024).length, 0);
  });
});

// ================================================================
// CV6xx Boot File Parser Tests
// ================================================================
describe('parseCv6xxBoot', () => {
  function buildTestFirmware(gslLen = 4096, tableCount = 2, tableSize = 1024, ubootLen = 8192) {
    const gslSize = gslLen + 3072;
    const paramsStart = gslSize + 1024;
    const ubootOffset = paramsStart + 1024;
    const totalSize = ubootOffset + ubootLen + 1024 + 40;
    const buf = new ArrayBuffer(totalSize);
    const data = new Uint8Array(buf);
    const view = new DataView(buf);

    // GSL magic at offset 2048
    view.setUint32(2048, 0x4BB4D22D, true);
    view.setUint32(2084, gslLen, true);

    // DDR params
    view.setUint32(paramsStart, 0x4B87A52D, true);
    view.setUint32(paramsStart + 32, 0, true); // offset_32
    view.setUint32(paramsStart + 36, tableSize, true);
    view.setUint32(paramsStart + 40, tableCount, true);
    for (let i = 0; i < 8; i++) data[paramsStart + 300 + i] = Math.min(i, tableCount - 1);

    // U-Boot magic
    view.setUint32(ubootOffset, 0x4BF01E2D, true);
    view.setUint32(ubootOffset + 36, ubootLen, true);

    return data;
  }

  it('parses valid firmware', () => {
    const fw = buildTestFirmware();
    const parts = parseCv6xxBoot(fw);
    assert.ok(parts.gslData.length > 0);
    assert.equal(parts.tableCount, 2);
    assert.equal(parts.tableSize, 1024);
    assert.ok(parts.ubootData.length > 0);
    assert.ok(parts.ddrTable.length > 0);
  });

  it('rejects invalid GSL magic', () => {
    const fw = buildTestFirmware();
    const view = new DataView(fw.buffer);
    view.setUint32(2048, 0xDEADBEEF, true);
    assert.throws(() => parseCv6xxBoot(fw), /Invalid GSL magic/);
  });

  it('rejects invalid DDR params magic', () => {
    const fw = buildTestFirmware();
    const gslLen = new DataView(fw.buffer).getUint32(2084, true);
    const gslSize = gslLen + 3072;
    const paramsStart = gslSize + 1024;
    new DataView(fw.buffer).setUint32(paramsStart, 0xBADBAD, true);
    assert.throws(() => parseCv6xxBoot(fw), /Invalid DDR params magic/);
  });

  it('rejects missing U-Boot magic', () => {
    // Create firmware with no U-Boot magic by zeroing it out
    const fw = buildTestFirmware();
    const gslLen = new DataView(fw.buffer).getUint32(2084, true);
    const gslSize = gslLen + 3072;
    const paramsStart = gslSize + 1024;
    const ubootOffset = paramsStart + 1024;
    new DataView(fw.buffer).setUint32(ubootOffset, 0x00000000, true);
    assert.throws(() => parseCv6xxBoot(fw), /U-Boot magic not found/);
  });

  it('returns correct DDR table size', () => {
    const fw = buildTestFirmware(4096, 3, 512, 8192);
    const parts = parseCv6xxBoot(fw);
    assert.equal(parts.tableCount, 3);
    assert.equal(parts.tableSize, 512);
    assert.equal(parts.ddrTable.length, 2048 + 512);
  });
});

// ================================================================
// SoC Lists Tests
// ================================================================
describe('SoC Lists', () => {
  it('V500_SOCS contains gk7205v500', () => {
    assert.ok(V500_SOCS.has('gk7205v500'));
  });

  it('V500_SOCS has 6 entries', () => {
    assert.equal(V500_SOCS.size, 6);
  });

  it('CV6XX_SOCS contains hi3516cv610', () => {
    assert.ok(CV6XX_SOCS.has('hi3516cv610'));
  });

  it('CV6XX_SOCS has 5 entries', () => {
    assert.equal(CV6XX_SOCS.size, 5);
  });

  it('V500 and CV6xx are disjoint', () => {
    for (const soc of V500_SOCS) assert.ok(!CV6XX_SOCS.has(soc));
  });
});
