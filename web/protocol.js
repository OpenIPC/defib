/**
 * defib protocol module — CRC-16, frame encoding, boot file parsing.
 * Shared between the web UI (index.html) and Node.js tests.
 */

// CRC-16/CCITT lookup table (polynomial 0x1021)
const CRC_TABLE = [
  0x0000,0x1021,0x2042,0x3063,0x4084,0x50a5,0x60c6,0x70e7,
  0x8108,0x9129,0xa14a,0xb16b,0xc18c,0xd1ad,0xe1ce,0xf1ef,
  0x1231,0x0210,0x3273,0x2252,0x52b5,0x4294,0x72f7,0x62d6,
  0x9339,0x8318,0xb37b,0xa35a,0xd3bd,0xc39c,0xf3ff,0xe3de,
  0x2462,0x3443,0x0420,0x1401,0x64e6,0x74c7,0x44a4,0x5485,
  0xa56a,0xb54b,0x8528,0x9509,0xe5ee,0xf5cf,0xc5ac,0xd58d,
  0x3653,0x2672,0x1611,0x0630,0x76d7,0x66f6,0x5695,0x46b4,
  0xb75b,0xa77a,0x9719,0x8738,0xf7df,0xe7fe,0xd79d,0xc7bc,
  0x48c4,0x58e5,0x6886,0x78a7,0x0840,0x1861,0x2802,0x3823,
  0xc9cc,0xd9ed,0xe98e,0xf9af,0x8948,0x9969,0xa90a,0xb92b,
  0x5af5,0x4ad4,0x7ab7,0x6a96,0x1a71,0x0a50,0x3a33,0x2a12,
  0xdbfd,0xcbdc,0xfbbf,0xeb9e,0x9b79,0x8b58,0xbb3b,0xab1a,
  0x6ca6,0x7c87,0x4ce4,0x5cc5,0x2c22,0x3c03,0x0c60,0x1c41,
  0xedae,0xfd8f,0xcdec,0xddcd,0xad2a,0xbd0b,0x8d68,0x9d49,
  0x7e97,0x6eb6,0x5ed5,0x4ef4,0x3e13,0x2e32,0x1e51,0x0e70,
  0xff9f,0xefbe,0xdfdd,0xcffc,0xbf1b,0xaf3a,0x9f59,0x8f78,
  0x9188,0x81a9,0xb1ca,0xa1eb,0xd10c,0xc12d,0xf14e,0xe16f,
  0x1080,0x00a1,0x30c2,0x20e3,0x5004,0x4025,0x7046,0x6067,
  0x83b9,0x9398,0xa3fb,0xb3da,0xc33d,0xd31c,0xe37f,0xf35e,
  0x02b1,0x1290,0x22f3,0x32d2,0x4235,0x5214,0x6277,0x7256,
  0xb5ea,0xa5cb,0x95a8,0x8589,0xf56e,0xe54f,0xd52c,0xc50d,
  0x34e2,0x24c3,0x14a0,0x0481,0x7466,0x6447,0x5424,0x4405,
  0xa7db,0xb7fa,0x8799,0x97b8,0xe75f,0xf77e,0xc71d,0xd73c,
  0x26d3,0x36f2,0x0691,0x16b0,0x6657,0x7676,0x4615,0x5634,
  0xd94c,0xc96d,0xf90e,0xe92f,0x99c8,0x89e9,0xb98a,0xa9ab,
  0x5844,0x4865,0x7806,0x6827,0x18c0,0x08e1,0x3882,0x28a3,
  0xcb7d,0xdb5c,0xeb3f,0xfb1e,0x8bf9,0x9bd8,0xabbb,0xbb9a,
  0x4a75,0x5a54,0x6a37,0x7a16,0x0af1,0x1ad0,0x2ab3,0x3a92,
  0xfd2e,0xed0f,0xdd6c,0xcd4d,0xbdaa,0xad8b,0x9de8,0x8dc9,
  0x7c26,0x6c07,0x5c64,0x4c45,0x3ca2,0x2c83,0x1ce0,0x0cc1,
  0xef1f,0xff3e,0xcf5d,0xdf7c,0xaf9b,0xbfba,0x8fd9,0x9ff8,
  0x6e17,0x7e36,0x4e55,0x5e74,0x2e93,0x3eb2,0x0ed1,0x1ef0,
];

function calcCrc(data, crc = 0) {
  for (const b of data) crc = ((crc << 8) | b) ^ CRC_TABLE[(crc >> 8) & 0xff];
  for (let i = 0; i < 2; i++) crc = ((crc << 8) | 0) ^ CRC_TABLE[(crc >> 8) & 0xff];
  return crc & 0xffff;
}

function appendCrc(data) {
  const crc = calcCrc(data);
  const out = new Uint8Array(data.length + 2);
  out.set(data);
  out[data.length] = (crc >> 8) & 0xff;
  out[data.length + 1] = crc & 0xff;
  return out;
}

function appendCrcLE(data) {
  const crc = calcCrc(data);
  const out = new Uint8Array(data.length + 2);
  out.set(data);
  out[data.length] = crc & 0xff;
  out[data.length + 1] = (crc >> 8) & 0xff;
  return out;
}

function verifyCrc(frame) {
  if (frame.length < 3) return false;
  const payload = frame.slice(0, frame.length - 2);
  const expected = (frame[frame.length - 2] << 8) | frame[frame.length - 1];
  return calcCrc(payload) === expected;
}

// Frame builders
function buildHeadFrame(length, address) {
  const f = new Uint8Array(12);
  f[0]=0xfe; f[1]=0x00; f[2]=0xff; f[3]=0x01;
  f[4]=(length>>24)&0xff; f[5]=(length>>16)&0xff; f[6]=(length>>8)&0xff; f[7]=length&0xff;
  f[8]=(address>>24)&0xff; f[9]=(address>>16)&0xff; f[10]=(address>>8)&0xff; f[11]=address&0xff;
  return appendCrc(f);
}

function buildDataFrame(seq, payload) {
  const f = new Uint8Array(3 + payload.length);
  f[0] = 0xda; f[1] = seq & 0xff; f[2] = (~seq) & 0xff;
  f.set(payload, 3);
  return appendCrc(f);
}

function buildTailFrame(seq) {
  const f = new Uint8Array(3);
  f[0] = 0xed; f[1] = seq & 0xff; f[2] = (~seq) & 0xff;
  return appendCrc(f);
}

function chunkData(data, size = 1024) {
  const chunks = [];
  for (let i = 0; i < data.length; i += size)
    chunks.push(data.slice(i, Math.min(i + size, data.length)));
  return chunks;
}

// CV6xx boot file parser
function parseCv6xxBoot(data) {
  const view = new DataView(data.buffer, data.byteOffset);
  // GSL
  const gslMagic = view.getUint32(2048, true);
  if (gslMagic !== 0x4BB4D22D) throw new Error(`Invalid GSL magic: 0x${gslMagic.toString(16)}`);
  const gslLen = view.getUint32(2084, true);
  const gslSize = gslLen + 3072;
  const gslData = data.slice(0, gslSize);
  // DDR params
  const paramsStart = gslSize + 1024;
  const paramsMagic = view.getUint32(paramsStart, true);
  if (paramsMagic !== 0x4B87A52D) throw new Error(`Invalid DDR params magic: 0x${paramsMagic.toString(16)}`);
  const offset32 = view.getUint32(paramsStart + 32, true);
  const tableSize = view.getUint32(paramsStart + 36, true);
  const tableCount = view.getUint32(paramsStart + 40, true);
  const boardMapping = data.slice(paramsStart + 300, paramsStart + 308);
  // U-Boot
  let ubootOffset = -1;
  for (let i = paramsStart; i < data.length - 4; i++) {
    if (data[i]===0x2d && data[i+1]===0x1e && data[i+2]===0xf0 && data[i+3]===0x4b) { ubootOffset = i; break; }
  }
  if (ubootOffset < 0) throw new Error('U-Boot magic not found');
  const ubootLen = view.getUint32(ubootOffset + 36, true);
  const ubootData = data.slice(ubootOffset, ubootOffset + ubootLen + 1024);
  // DDR table for board_id=0
  const mappedIdx = Math.min(boardMapping[0], tableCount - 1);
  const ddrBuf = new Uint8Array(2048 + tableSize);
  ddrBuf.set(data.slice(gslSize, gslSize + 2048));
  const tblOff = gslSize + 2048 + offset32 + mappedIdx * tableSize;
  ddrBuf.set(data.slice(tblOff, tblOff + tableSize), 2048);

  return { gslData, gslSize, ddrTable: ddrBuf, ubootData, tableCount, tableSize };
}

// V500/CV6xx SoC lists
const V500_SOCS = new Set(["gk7205v500","gk7205v510","gk7205v530","xm7205v500","xm7205v510","xm7205v530"]);
const CV6XX_SOCS = new Set(["hi3516cv608","hi3516cv610","hi3516cv613","hi3516dv500","hi3519dv500"]);

// ================================================================
// OpenIPC U-Boot asset resolution
//
// GitHub release assets cannot be fetched from a browser: the
// download URL 302s to release-assets.githubusercontent.com, which
// sends no Access-Control-Allow-Origin, so the redirect is blocked
// (issue #113). The bytes therefore have to come through a public
// CORS proxy, which is untrusted — it can return anything.
//
// api.github.com *is* CORS-enabled and publishes a SHA-256 digest
// for every asset, so we take the hash over the trusted channel and
// verify the proxied bytes against it. A proxy that substitutes,
// truncates or rate-limits is caught before anything is flashed.
// ================================================================

const FW_RELEASE_API =
  'https://api.github.com/repos/OpenIPC/firmware/releases/tags/latest';
const FW_DIRECT_BASE =
  'https://github.com/OpenIPC/firmware/releases/download/latest';

// Asset name → chip, e.g. u-boot-hi3516ev300-universal.bin → hi3516ev300
const FW_ASSET_RE = /^u-boot-(.+)-universal\.bin$/;

// Chip aliases: chips whose U-Boot binary is published under another name.
const CHIP_FW_ALIAS = {
  "hi3518ev201": "hi3518ev200",
  "gk7201v200": "gk7205v200",
  "gk7201v300": "gk7205v200",
  "gk7205v210": "gk7205v200",
};

function fwNameForChip(chip) {
  // Drop any ":variant" suffix — U-Boot binaries are per-chip, not per-board.
  const base = String(chip).split(':', 1)[0];
  return CHIP_FW_ALIAS[base] || base;
}

/**
 * Build chip → {name, url, size, sha256} from a GitHub release API response.
 * Only u-boot-*-universal.bin assets are considered. `digest` is present on
 * modern GitHub responses as "sha256:<hex>"; it may be missing on older ones,
 * in which case sha256 is null and the caller falls back to a size check.
 */
function parseReleaseAssets(release) {
  const out = new Map();
  for (const asset of (release && release.assets) || []) {
    const m = FW_ASSET_RE.exec(asset.name || '');
    if (!m) continue;
    out.set(m[1], {
      name: asset.name,
      url: `${FW_DIRECT_BASE}/${asset.name}`,
      size: asset.size,
      sha256: parseDigest(asset.digest),
    });
  }
  return out;
}

/** "sha256:abc..." → "abc..." (lowercase hex), else null. */
function parseDigest(digest) {
  const m = /^sha256:([0-9a-f]{64})$/i.exec(String(digest || ''));
  return m ? m[1].toLowerCase() : null;
}

// OpenIPC's own Cloudflare Worker (github.com/OpenIPC/cors-proxy), shared with
// rnd-player. It fetches upstream without Origin/Referer, so GitHub's asset
// host serves it as a plain server-to-server request, and it follows the 302
// to release-assets.githubusercontent.com transparently.
//
// Two layers gate it: the request Origin must be in the worker's allowlist
// (https://openipc.github.io already is — rnd-player shares the origin), and
// non-localhost origins must present an HMAC-SHA256 signature. Localhost is
// exempt, so local development needs no key.
const PROXY_WINDOW_SECONDS = 300;

/** Signed message for the worker: `floor(t/300):url`, matching auth.ts. */
function proxySignatureMessage(targetUrl, tSeconds) {
  return `${Math.floor(tSeconds / PROXY_WINDOW_SECONDS)}:${targetUrl}`;
}

/**
 * HMAC-SHA256 as hex. The worker signs with the raw bytes of the key *string*
 * (TextEncoder over the hex characters), not the decoded hex — mirror that
 * exactly or every signature is rejected.
 */
async function hmacSha256Hex(secret, message) {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    'raw', enc.encode(secret), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign'],
  );
  const sig = await crypto.subtle.sign('HMAC', key, enc.encode(message));
  return bytesToHex(new Uint8Array(sig));
}

/** Build the worker URL; omit t/sig to send unsigned (localhost only). */
function buildOpenIpcProxyUrl(base, targetUrl, tSeconds, signature) {
  const params = [`url=${encodeURIComponent(targetUrl)}`];
  if (tSeconds != null && signature) params.push(`t=${tSeconds}`, `sig=${signature}`);
  return `${String(base).replace(/\/+$/, '')}/proxy?${params.join('&')}`;
}

/**
 * Public CORS proxies, tried in order. Every one of these is an unaffiliated
 * third party, which is only acceptable because the SHA-256 check makes them
 * untrusted transport rather than a trusted source.
 *
 * Measured 2026-07-28 against u-boot-gk7205v300-universal.bin (250 KB):
 *   corsproxy.io      403 "This domain has been blocked and can't be proxied"
 *   api.allorigins.win 500/522 on binaries this size
 *   api.codetabs.com  522, api.cors.lol 429 (rate limited)
 *   proxy.corsfix.com / test.cors.workers.dev  403 (need registered origin)
 *   cors.eu.org       200, correct digest, ACAO * — the only one that worked
 */
const FW_PROXIES = [
  { name: 'cors.eu.org', build: (url) => `https://cors.eu.org/${url}` },
  {
    name: 'allorigins',
    build: (url) => `https://api.allorigins.win/raw?url=${encodeURIComponent(url)}`,
  },
  {
    name: 'codetabs',
    build: (url) => `https://api.codetabs.com/v1/proxy?quest=${encodeURIComponent(url)}`,
  },
];

/**
 * Ordered download attempts for an asset URL. Direct comes first so the tool
 * stops depending on proxies the moment GitHub restores CORS (and so it works
 * unchanged for anyone running behind their own proxy or extension). The
 * OpenIPC worker is the intended workhorse; the public proxies remain only as
 * a last resort, since they rate-limit aggressively.
 */
function fwSourceUrls(url, openIpcProxyUrl) {
  return [
    { name: 'github.com (direct)', url },
    ...(openIpcProxyUrl ? [{ name: 'OpenIPC cors-proxy', url: openIpcProxyUrl }] : []),
    ...FW_PROXIES.map((p) => ({ name: p.name, url: p.build(url) })),
  ];
}

/** Uint8Array → lowercase hex string. */
function bytesToHex(bytes) {
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/**
 * Check downloaded bytes against release metadata.
 * Returns {ok: true} or {ok: false, reason: string}.
 * `actualSha` is null when SubtleCrypto is unavailable (insecure context).
 */
function verifyFirmwareBytes(bytes, meta, actualSha) {
  if (!bytes || bytes.length === 0) return { ok: false, reason: 'empty response' };
  if (meta && meta.size && bytes.length !== meta.size) {
    return { ok: false, reason: `size ${bytes.length} != expected ${meta.size}` };
  }
  // An HTML error page or JSON rate-limit body that happens to match on size
  // still cannot match the digest; this catches the no-digest case.
  if (bytes[0] === 0x3c) return { ok: false, reason: 'response looks like HTML, not a binary' };
  if (meta && meta.sha256 && actualSha && actualSha !== meta.sha256) {
    return { ok: false, reason: `SHA-256 ${actualSha.slice(0, 16)}… != expected ${meta.sha256.slice(0, 16)}…` };
  }
  return { ok: true };
}

// Export for Node.js (no-op in browser)
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    CRC_TABLE, calcCrc, appendCrc, appendCrcLE, verifyCrc,
    buildHeadFrame, buildDataFrame, buildTailFrame, chunkData,
    parseCv6xxBoot, V500_SOCS, CV6XX_SOCS,
    FW_RELEASE_API, FW_DIRECT_BASE, FW_ASSET_RE, FW_PROXIES,
    CHIP_FW_ALIAS, fwNameForChip, parseReleaseAssets, parseDigest,
    fwSourceUrls, bytesToHex, verifyFirmwareBytes,
    PROXY_WINDOW_SECONDS, proxySignatureMessage, hmacSha256Hex,
    buildOpenIpcProxyUrl,
  };
}
