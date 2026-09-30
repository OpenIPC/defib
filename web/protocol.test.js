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
  parseCv6xxBoot, CV6XX_GSL_LOAD_ADDR, V500_SOCS, CV6XX_SOCS,
  FW_DIRECT_BASE, FW_PROXIES, fwNameForChip, parseReleaseAssets,
  parseDigest, fwSourceUrls, bytesToHex, verifyFirmwareBytes,
  PROXY_WINDOW_SECONDS, proxySignatureMessage, hmacSha256Hex,
  buildOpenIpcProxyUrl,
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
  function buildTestFirmware(options = {}) {
    const layout = {
      gslHeaderOffset: 0x800,
      gslStructureLength: 0x400,
      gslLength: 0x1000,
      reeKeyLength: 0x400,
      paramsStructureLength: 0x400,
      paramsAreaOffset: 0,
      tableCount: 2,
      tableSize: 0x400,
      ubootStructureLength: 0x400,
      ubootLength: 0x2000,
      ...options,
    };
    const gslEnd = layout.gslHeaderOffset + layout.gslStructureLength + layout.gslLength;
    const paramsStart = gslEnd + layout.reeKeyLength;
    const firstTable = paramsStart + layout.paramsStructureLength + layout.paramsAreaOffset;
    const ubootOffset = firstTable + layout.tableCount * layout.tableSize;
    const data = new Uint8Array(
      ubootOffset + layout.ubootStructureLength + layout.ubootLength,
    );
    const view = new DataView(data.buffer);

    view.setUint32(layout.gslHeaderOffset, 0x4BB4D22D, true);
    view.setUint32(layout.gslHeaderOffset + 4, 0x100, true);
    view.setUint32(layout.gslHeaderOffset + 8, layout.gslStructureLength, true);
    view.setUint32(layout.gslHeaderOffset + 12, 0x40, true);
    view.setUint32(layout.gslHeaderOffset + 36, layout.gslLength, true);

    view.setUint32(paramsStart, 0x4B87A52D, true);
    view.setUint32(paramsStart + 4, 0x100, true);
    view.setUint32(paramsStart + 8, layout.paramsStructureLength, true);
    view.setUint32(paramsStart + 12, 0x40, true);
    view.setUint32(paramsStart + 32, layout.paramsAreaOffset, true);
    view.setUint32(paramsStart + 36, layout.tableSize, true);
    view.setUint32(paramsStart + 40, layout.tableCount, true);
    for (let i = 0; i < 8; i++) {
      data[paramsStart + 300 + i] = i < layout.tableCount ? i : 0xff;
    }
    for (let i = 0; i < layout.tableCount; i++) {
      data.fill(i + 1, firstTable + i * layout.tableSize, firstTable + (i + 1) * layout.tableSize);
    }

    view.setUint32(ubootOffset, 0x4BF01E2D, true);
    view.setUint32(ubootOffset + 4, 0x100, true);
    view.setUint32(ubootOffset + 8, layout.ubootStructureLength, true);
    view.setUint32(ubootOffset + 12, 0x40, true);
    view.setUint32(ubootOffset + 36, layout.ubootLength, true);

    return { data, layout, gslEnd, paramsStart, firstTable, ubootOffset };
  }

  for (const [name, options] of [
    ['0x800', {}],
    ['0x1200', {
      gslHeaderOffset: 0x1200,
      gslStructureLength: 0x200,
      reeKeyLength: 0x100,
      paramsStructureLength: 0x200,
      paramsAreaOffset: 0x100,
      ubootStructureLength: 0x200,
    }],
  ]) {
    it(`parses valid ${name} firmware layout`, () => {
      const { data, layout, gslEnd } = buildTestFirmware(options);
      const parts = parseCv6xxBoot(data);
      assert.equal(parts.gslData.length, gslEnd);
      assert.equal(parts.tableCount, layout.tableCount);
      assert.equal(parts.tableSize, layout.tableSize);
      assert.equal(parts.ubootData.length, layout.ubootStructureLength + layout.ubootLength);
    });
  }

  it('exports the CP_STEP1 GSL load address', () => {
    assert.equal(CV6XX_GSL_LOAD_ADDR, 0x04021A00);
  });

  it('rejects invalid GSL magic', () => {
    const { data, layout } = buildTestFirmware();
    new DataView(data.buffer).setUint32(layout.gslHeaderOffset, 0xDEADBEEF, true);
    assert.throws(() => parseCv6xxBoot(data), /No structurally valid/);
  });

  it('ignores a plausible false GSL header before a valid layout', () => {
    const { data, gslEnd } = buildTestFirmware({ gslHeaderOffset: 0x1200 });
    const view = new DataView(data.buffer);
    view.setUint32(0x800, 0x4BB4D22D, true);
    view.setUint32(0x804, 0x100, true);
    view.setUint32(0x808, 0x200, true);
    view.setUint32(0x80c, 0x40, true);
    view.setUint32(0x824, 0x200, true);

    assert.equal(parseCv6xxBoot(data).gslSize, gslEnd);
  });

  it('rejects truncated DDR tables', () => {
    const { data, paramsStart } = buildTestFirmware();
    new DataView(data.buffer).setUint32(paramsStart + 36, data.length, true);
    assert.throws(() => parseCv6xxBoot(data), /No structurally valid/);
  });

  it('returns correct DDR table size', () => {
    const { data } = buildTestFirmware({ tableCount: 3, tableSize: 0x200 });
    const parts = parseCv6xxBoot(data);
    assert.equal(parts.tableCount, 3);
    assert.equal(parts.tableSize, 0x200);
    assert.equal(parts.ddrTable.length, 0x800 + 0x200);
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

// ================================================================
// U-Boot asset resolution (issue #113)
// ================================================================

// Trimmed shape of api.github.com .../releases/tags/latest, with the real
// digest and size of u-boot-gk7205v300-universal.bin.
const RELEASE_FIXTURE = {
  tag_name: 'latest',
  assets: [
    {
      name: 'u-boot-gk7205v300-universal.bin',
      size: 256459,
      digest: 'sha256:0160bbcb7b8e40a13abbc96d34bc26575534e0ea0d6cc4a5b947ce0bd55ce3dc',
    },
    { name: 'u-boot-hi3520dv200-universal.bin', size: 262144, digest: null },
    // Non-U-Boot assets and the CV6xx boot images must be ignored.
    { name: 'openipc.hi3516ev300-nor-ultimate.tgz', size: 10315993, digest: 'sha256:' + 'a'.repeat(64) },
    { name: 'boot-hi3516cv608-nor.bin', size: 241664, digest: 'sha256:' + 'b'.repeat(64) },
  ],
};

describe('parseReleaseAssets', () => {
  it('keeps only u-boot-*-universal.bin assets', () => {
    const m = parseReleaseAssets(RELEASE_FIXTURE);
    assert.deepEqual([...m.keys()].sort(), ['gk7205v300', 'hi3520dv200']);
  });

  it('extracts size and sha256 digest', () => {
    const a = parseReleaseAssets(RELEASE_FIXTURE).get('gk7205v300');
    assert.equal(a.size, 256459);
    assert.equal(a.sha256, '0160bbcb7b8e40a13abbc96d34bc26575534e0ea0d6cc4a5b947ce0bd55ce3dc');
    assert.equal(a.url, `${FW_DIRECT_BASE}/u-boot-gk7205v300-universal.bin`);
  });

  it('tolerates a missing digest', () => {
    assert.equal(parseReleaseAssets(RELEASE_FIXTURE).get('hi3520dv200').sha256, null);
  });

  it('returns an empty map for a malformed response', () => {
    assert.equal(parseReleaseAssets(null).size, 0);
    assert.equal(parseReleaseAssets({}).size, 0);
  });
});

describe('parseDigest', () => {
  it('accepts sha256:<64 hex>', () => {
    assert.equal(parseDigest('sha256:' + 'A'.repeat(64)), 'a'.repeat(64));
  });

  it('rejects other algorithms, wrong lengths and junk', () => {
    assert.equal(parseDigest('md5:' + 'a'.repeat(32)), null);
    assert.equal(parseDigest('sha256:abc'), null);
    assert.equal(parseDigest(undefined), null);
  });
});

describe('fwNameForChip', () => {
  it('maps aliases to the published binary name', () => {
    assert.equal(fwNameForChip('hi3518ev201'), 'hi3518ev200');
    assert.equal(fwNameForChip('gk7201v300'), 'gk7205v200');
  });

  it('passes unknown chips through unchanged', () => {
    assert.equal(fwNameForChip('hi3516ev300'), 'hi3516ev300');
  });

  it('strips a :variant suffix', () => {
    assert.equal(fwNameForChip('hi3516ev300:neo'), 'hi3516ev300');
  });
});

describe('fwSourceUrls', () => {
  const url = `${FW_DIRECT_BASE}/u-boot-gk7205v300-universal.bin`;

  it('tries the direct GitHub URL first', () => {
    assert.equal(fwSourceUrls(url)[0].url, url);
  });

  it('falls back to every configured proxy', () => {
    assert.equal(fwSourceUrls(url).length, 1 + FW_PROXIES.length);
  });

  it('percent-encodes the target for query-style proxies', () => {
    const allorigins = fwSourceUrls(url).find((s) => s.name === 'allorigins');
    assert.ok(allorigins.url.includes(encodeURIComponent(url)));
    assert.ok(!allorigins.url.includes('?url=https://'));
  });
});

describe('verifyFirmwareBytes', () => {
  const meta = { size: 4, sha256: 'c'.repeat(64) };
  const good = new Uint8Array([0xde, 0xad, 0xbe, 0xef]);

  it('accepts bytes matching size and digest', () => {
    assert.deepEqual(verifyFirmwareBytes(good, meta, 'c'.repeat(64)), { ok: true });
  });

  it('rejects a digest mismatch — a proxy substituting bytes', () => {
    const r = verifyFirmwareBytes(good, meta, 'd'.repeat(64));
    assert.equal(r.ok, false);
    assert.match(r.reason, /SHA-256/);
  });

  it('rejects a truncated download', () => {
    const r = verifyFirmwareBytes(good.slice(0, 2), meta, 'c'.repeat(64));
    assert.equal(r.ok, false);
    assert.match(r.reason, /size/);
  });

  it('rejects an empty response', () => {
    assert.equal(verifyFirmwareBytes(new Uint8Array(0), meta, null).ok, false);
  });

  it('rejects an HTML error page even with no digest to check', () => {
    const html = new Uint8Array([0x3c, 0x68, 0x74, 0x6d]); // "<htm"
    const r = verifyFirmwareBytes(html, { size: 4, sha256: null }, null);
    assert.equal(r.ok, false);
    assert.match(r.reason, /HTML/);
  });

  it('falls back to a size check when SubtleCrypto is unavailable', () => {
    assert.deepEqual(verifyFirmwareBytes(good, meta, null), { ok: true });
  });
});

describe('bytesToHex', () => {
  it('zero-pads each byte to two digits', () => {
    assert.equal(bytesToHex(new Uint8Array([0x00, 0x0f, 0xff])), '000fff');
  });
});

// ================================================================
// OpenIPC cors-proxy worker integration
// ================================================================

const PROXY_BASE = 'https://cors-proxy.joseph-nef.workers.dev';
const TARGET = `${FW_DIRECT_BASE}/u-boot-gk7205v300-universal.bin`;

describe('proxySignatureMessage', () => {
  it('is `floor(t/300):url`, matching the worker', () => {
    assert.equal(proxySignatureMessage(TARGET, 1_785_242_320), `5950807:${TARGET}`);
  });

  it('is stable across a 5-minute window and changes at the boundary', () => {
    const base = 5_950_800 * PROXY_WINDOW_SECONDS;
    assert.equal(proxySignatureMessage(TARGET, base), proxySignatureMessage(TARGET, base + 299));
    assert.notEqual(proxySignatureMessage(TARGET, base), proxySignatureMessage(TARGET, base + 300));
  });

  it('covers the target url, so a swapped target changes the message', () => {
    assert.notEqual(
      proxySignatureMessage(TARGET, 1_785_242_320),
      proxySignatureMessage('https://evil.example/x.bin', 1_785_242_320),
    );
  });
});

describe('hmacSha256Hex', () => {
  it('matches a known HMAC-SHA256 vector over the key string bytes', async () => {
    // The worker signs with TextEncoder over the key characters, not decoded
    // hex — node's createHmac with a utf8 key is the same thing.
    const { createHmac } = require('node:crypto');
    const key = 'a'.repeat(64);
    const msg = `5950807:${TARGET}`;
    const expected = createHmac('sha256', key).update(msg).digest('hex');
    assert.equal(await hmacSha256Hex(key, msg), expected);
  });

  it('returns 64 hex chars', async () => {
    assert.match(await hmacSha256Hex('k', 'm'), /^[0-9a-f]{64}$/);
  });
});

describe('buildOpenIpcProxyUrl', () => {
  it('percent-encodes the target and attaches t and sig', () => {
    const u = new URL(buildOpenIpcProxyUrl(PROXY_BASE, TARGET, 1234, 'deadbeef'));
    assert.equal(u.pathname, '/proxy');
    assert.equal(u.searchParams.get('url'), TARGET);
    assert.equal(u.searchParams.get('t'), '1234');
    assert.equal(u.searchParams.get('sig'), 'deadbeef');
  });

  it('omits t and sig when unsigned (localhost is HMAC-exempt)', () => {
    const u = new URL(buildOpenIpcProxyUrl(PROXY_BASE, TARGET, null, null));
    assert.equal(u.searchParams.get('url'), TARGET);
    assert.equal(u.searchParams.get('t'), null);
    assert.equal(u.searchParams.get('sig'), null);
  });

  it('does not double up slashes on a trailing-slash base', () => {
    assert.ok(buildOpenIpcProxyUrl(PROXY_BASE + '/', TARGET, null, null).includes('.dev/proxy?'));
  });
});

describe('fwSourceUrls with the OpenIPC worker', () => {
  it('puts the worker second, after the direct attempt', () => {
    const sources = fwSourceUrls(TARGET, `${PROXY_BASE}/proxy?url=x`);
    assert.equal(sources[0].name, 'github.com (direct)');
    assert.equal(sources[1].name, 'OpenIPC cors-proxy');
  });

  it('keeps the public proxies as a last resort behind it', () => {
    const sources = fwSourceUrls(TARGET, `${PROXY_BASE}/proxy?url=x`);
    assert.equal(sources.length, 2 + FW_PROXIES.length);
    assert.deepEqual(sources.slice(2).map((s) => s.name), FW_PROXIES.map((p) => p.name));
  });

  it('is omitted entirely when the proxy is not configured', () => {
    const sources = fwSourceUrls(TARGET, null);
    assert.ok(!sources.some((s) => s.name === 'OpenIPC cors-proxy'));
    assert.equal(sources.length, 1 + FW_PROXIES.length);
  });
});
