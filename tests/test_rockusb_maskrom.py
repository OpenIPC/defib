"""Tests for MaskROM control-transfer framing.

The size quirks around the 4096-byte chunk boundary are the whole reason this
framing lives in a pure function — they are easy to get wrong and impossible
to notice without hardware, because a mis-framed loader simply never comes
back.
"""

import struct

import pytest

from defib.rockusb.codec import rc4, rk_crc16
from defib.rockusb.maskrom import CHUNK_SIZE, build_maskrom_chunks


def _rejoin(chunks: list[bytes]) -> bytes:
    return b"".join(chunks)


class TestChunking:
    @pytest.mark.parametrize("size", [1, 100, 4095, 4096, 4097, 8192, 100_000])
    def test_no_chunk_exceeds_limit(self, size):
        chunks = build_maskrom_chunks(b"\xa5" * size)
        assert all(len(c) <= CHUNK_SIZE for c in chunks)

    def test_small_blob_is_one_chunk(self):
        chunks = build_maskrom_chunks(b"\x01\x02\x03")
        assert len(chunks) == 1
        assert len(chunks[0]) == 5  # 3 payload + 2 CRC

    def test_crc_appended_big_endian(self):
        blob = b"\xde\xad\xbe\xef"
        chunks = build_maskrom_chunks(blob)
        assert _rejoin(chunks) == blob + struct.pack(">H", rk_crc16(blob))

    def test_exact_chunk_multiple_spills_crc(self):
        blob = b"\x11" * CHUNK_SIZE
        chunks = build_maskrom_chunks(blob)
        assert len(chunks) == 2
        assert len(chunks[0]) == CHUNK_SIZE
        assert len(chunks[1]) == 2  # just the CRC


class TestBoundaryQuirks:
    def test_4095_pads_before_crc(self):
        """A 4095-byte blob gains a zero byte so the CRC stays contiguous."""
        blob = b"\x5a" * (CHUNK_SIZE - 1)
        chunks = build_maskrom_chunks(blob)

        padded = blob + b"\x00"
        assert _rejoin(chunks) == padded + struct.pack(">H", rk_crc16(padded))
        # Padding matters: the CRC must cover it, not the unpadded blob.
        assert rk_crc16(padded) != rk_crc16(blob)
        assert len(chunks[0]) == CHUNK_SIZE
        assert len(chunks[1]) == 2

    def test_4094_appends_terminator_packet(self):
        """CRC lands the payload on an exact multiple, so a short packet is
        needed to close the transfer."""
        blob = b"\x5a" * (CHUNK_SIZE - 2)
        chunks = build_maskrom_chunks(blob)

        assert len(chunks) == 2
        assert len(chunks[0]) == CHUNK_SIZE
        assert chunks[-1] == b"\x00"
        assert chunks[0] == blob + struct.pack(">H", rk_crc16(blob))

    def test_4094_plus_full_chunk_also_terminated(self):
        blob = b"\x5a" * (CHUNK_SIZE + CHUNK_SIZE - 2)
        chunks = build_maskrom_chunks(blob)
        assert chunks[-1] == b"\x00"
        assert sum(len(c) for c in chunks[:-1]) % CHUNK_SIZE == 0

    @pytest.mark.parametrize("size", [4093, 4096, 4097])
    def test_no_terminator_when_not_needed(self, size):
        chunks = build_maskrom_chunks(b"\x5a" * size)
        assert chunks[-1] != b"\x00" or len(chunks[-1]) > 1


class TestRc4Path:
    def test_rc4_off_by_default(self):
        """RV1106 loaders are built RC4-off; defaulting the other way would
        silently corrupt every upload."""
        blob = b"\x01" * 64
        assert _rejoin(build_maskrom_chunks(blob)).startswith(blob)

    def test_rc4_on_encrypts_then_checksums(self):
        blob = b"\x01" * 64
        chunks = build_maskrom_chunks(blob, use_rc4=True)

        encrypted = rc4(blob)
        assert _rejoin(chunks) == encrypted + struct.pack(">H", rk_crc16(encrypted))

    def test_rc4_changes_output(self):
        blob = b"\x01" * 64
        assert build_maskrom_chunks(blob) != build_maskrom_chunks(blob, use_rc4=True)


class TestDeterminism:
    def test_stable_across_calls(self):
        blob = bytes(range(256)) * 40
        assert build_maskrom_chunks(blob) == build_maskrom_chunks(blob)

    def test_input_not_mutated(self):
        blob = bytearray(b"\x01\x02\x03")
        build_maskrom_chunks(bytes(blob))
        assert blob == bytearray(b"\x01\x02\x03")
