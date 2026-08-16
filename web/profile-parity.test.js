/**
 * Parity between the browser build's inline PROFILES blob and the CLI's
 * profile data under src/defib/profiles/data.
 *
 * web/index.html carries its own hand-maintained copy of the SoC profiles so
 * the page can be deployed as static files. Nothing regenerates it, so it can
 * drift from the CLI — which is exactly how every chip in it ended up with its
 * PRESTEP0 stripped while staying selectable in the dropdown (defib#121).
 *
 * These tests fail when that happens again.
 *
 * Run: node --test web/profile-parity.test.js
 */

const { describe, it } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const { FRAME_BLAST_SOCS } = require('./protocol.js');

const REPO_ROOT = path.join(__dirname, '..');
const DATA_DIR = path.join(REPO_ROOT, 'src', 'defib', 'profiles', 'data');
const MAX_ALIAS_DEPTH = 23;

/**
 * Resolve a chip to its profile object, following alias files.
 *
 * An alias is a profile file whose entire contents are a single token ending
 * in `.json` — e.g. hi3516ev300.json contains just "hi3516ev200.json". Missing
 * this indirection is what made the PRESTEP0 drift invisible: a naive read of
 * hi3516ev300.json finds no PRESTEP0 because it holds no fields at all.
 * Mirrors load_profile() in src/defib/profiles/loader.py.
 */
function resolveProfile(chip, depth = 0) {
  if (depth > MAX_ALIAS_DEPTH) return null;
  const file = path.join(DATA_DIR, `${chip}.json`);
  if (!fs.existsSync(file)) return null;
  const raw = fs.readFileSync(file, 'utf8').trim();
  const tokens = raw.split(/\s+/);
  if (tokens.length === 1 && tokens[0].endsWith('.json')) {
    return resolveProfile(tokens[0].slice(0, -5), depth + 1);
  }
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/** Pull the inline `const PROFILES = {...};` blob out of web/index.html. */
function readWebProfiles() {
  const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
  const m = html.match(/const PROFILES = (\{[\s\S]*?\});\n/);
  assert.ok(m, 'could not locate the PROFILES blob in web/index.html');
  return JSON.parse(m[1]);
}

describe('web PROFILES vs CLI profile data', () => {
  const webProfiles = readWebProfiles();

  it('every chip in the web dropdown has a CLI profile behind it', () => {
    const orphans = Object.keys(webProfiles).filter(c => resolveProfile(c) === null);
    assert.deepEqual(orphans, [],
      `web-only chips with no resolvable CLI profile: ${orphans.join(', ')}`);
  });

  it('FRAME_BLAST_SOCS matches the chips whose resolved profile has PRESTEP0', () => {
    const expected = Object.keys(webProfiles)
      .filter(c => {
        const p = resolveProfile(c);
        return p && p.PRESTEP0 != null;
      })
      .sort();
    const actual = [...FRAME_BLAST_SOCS].sort();

    const missing = expected.filter(c => !FRAME_BLAST_SOCS.has(c));
    const extra = actual.filter(c => !expected.includes(c));

    assert.deepEqual(missing, [],
      `these chips need a frame-blast handshake but are not in FRAME_BLAST_SOCS, ` +
      `so the UI will let users start a recovery that cannot succeed: ${missing.join(', ')}`);
    assert.deepEqual(extra, [],
      `these are listed in FRAME_BLAST_SOCS but their resolved profile has no ` +
      `PRESTEP0, so they are being blocked for no reason: ${extra.join(', ')}`);
  });

  it('the web build does not claim to send PRESTEP0 it has no data for', () => {
    // If a future change starts shipping PRESTEP0 in the web blob, the
    // frame-blast path must be implemented in protocol.js at the same time —
    // otherwise the data is inert and the block list above silently wrong.
    const withPrestep = Object.entries(webProfiles)
      .filter(([, p]) => p.PRESTEP0 != null)
      .map(([c]) => c);
    const protocolSrc = fs.readFileSync(path.join(__dirname, 'protocol.js'), 'utf8');
    const implemented = /function\s+buildPrestepFrames|sendFrameForStart/.test(protocolSrc);
    if (withPrestep.length > 0) {
      assert.ok(implemented,
        `web PROFILES now carry PRESTEP0 for ${withPrestep.join(', ')} but ` +
        `protocol.js still has no frame-blast implementation — either implement ` +
        `it or drop the data`);
    }
  });
});
