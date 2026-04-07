#!/usr/bin/env python3
"""Unit tests for jcz_protocol.py — JCZ/BJJCZ command parser."""

import struct
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from jcz_protocol import (
    JCZOp, JCZCommand, parse_chunk, parse_stream, build_chunk,
    galvo_to_mm, mm_to_galvo,
    CHUNK_SIZE, COMMAND_SIZE, COMMANDS_PER_CHUNK,
    DEFAULT_FIELD_SIZE_MM,
)


class TestJCZCommand(unittest.TestCase):
    """JCZ command creation and property tests."""

    def test_nop(self):
        cmd = JCZCommand(JCZOp.NOP)
        self.assertTrue(cmd.is_nop)
        self.assertFalse(cmd.is_mark)
        self.assertFalse(cmd.is_travel)
        self.assertIsNone(cmd.xy_galvo)

    def test_travel(self):
        # p1=Y=0x4000, p2=X=0x6000
        cmd = JCZCommand(JCZOp.TRAVEL, p1=0x4000, p2=0x6000)
        self.assertTrue(cmd.is_travel)
        self.assertTrue(cmd.is_movement)
        xy = cmd.xy_galvo
        self.assertEqual(xy, (0x6000, 0x4000))  # (X, Y)

    def test_mark(self):
        cmd = JCZCommand(JCZOp.MARK, p1=0x8000, p2=0x8000)
        self.assertTrue(cmd.is_mark)
        self.assertTrue(cmd.is_movement)
        xy = cmd.xy_galvo
        self.assertEqual(xy, (0x8000, 0x8000))  # centre

    def test_laser_switch_on(self):
        cmd = JCZCommand(JCZOp.LASER_SWITCH, p1=1)
        self.assertTrue(cmd.is_laser_on)

    def test_laser_switch_off(self):
        cmd = JCZCommand(JCZOp.LASER_SWITCH, p1=0)
        self.assertFalse(cmd.is_laser_on)

    def test_non_laser_switch_returns_none(self):
        cmd = JCZCommand(JCZOp.NOP)
        self.assertIsNone(cmd.is_laser_on)

    def test_to_bytes_roundtrip(self):
        cmd = JCZCommand(0x8005, 0x1234, 0x5678, 0x9ABC, 0xDEF0, 0x0001)
        raw = cmd.to_bytes()
        self.assertEqual(len(raw), COMMAND_SIZE)
        vals = struct.unpack('<HHHHHH', raw)
        self.assertEqual(vals, (0x8005, 0x1234, 0x5678, 0x9ABC, 0xDEF0, 0x0001))

    def test_repr_known_opcode(self):
        cmd = JCZCommand(JCZOp.TRAVEL, p1=1, p2=2)
        r = repr(cmd)
        self.assertIn('TRAVEL', r)

    def test_repr_unknown_opcode(self):
        cmd = JCZCommand(0xBEEF)
        r = repr(cmd)
        self.assertIn('0xbeef', r)


class TestChunkParsing(unittest.TestCase):
    """Chunk and stream parsing tests."""

    def _make_chunk(self, commands):
        """Build a valid 3072-byte chunk from a list of commands."""
        data = bytearray()
        for cmd in commands:
            data += cmd.to_bytes()
        nop = JCZCommand(JCZOp.NOP).to_bytes()
        while len(data) < CHUNK_SIZE:
            data += nop
        return bytes(data)

    def test_parse_empty_chunk(self):
        """All-NOP chunk parses into 256 NOP commands."""
        data = bytes(CHUNK_SIZE)  # all zeros — not NOP but still parseable
        commands = parse_chunk(data)
        self.assertEqual(len(commands), COMMANDS_PER_CHUNK)

    def test_parse_chunk_wrong_size(self):
        with self.assertRaises(ValueError):
            parse_chunk(b'\x00' * 100)

    def test_parse_chunk_with_commands(self):
        travel = JCZCommand(JCZOp.TRAVEL, p1=0x8000, p2=0x8000)
        mark   = JCZCommand(JCZOp.MARK,   p1=0x9000, p2=0x9000)
        chunk  = self._make_chunk([travel, mark])
        result = parse_chunk(chunk)

        self.assertEqual(result[0].opcode, JCZOp.TRAVEL)
        self.assertEqual(result[0].p1, 0x8000)
        self.assertEqual(result[1].opcode, JCZOp.MARK)
        self.assertEqual(result[1].p1, 0x9000)
        # Remaining should be NOPs
        self.assertTrue(result[2].is_nop)

    def test_parse_stream_single_chunk(self):
        chunk = self._make_chunk([JCZCommand(JCZOp.TRAVEL, p1=100, p2=200)])
        result = parse_stream(chunk)
        self.assertEqual(len(result), COMMANDS_PER_CHUNK)

    def test_parse_stream_multiple_chunks(self):
        chunk = self._make_chunk([JCZCommand(JCZOp.MARK, p1=1, p2=2)])
        data = chunk + chunk + chunk
        result = parse_stream(data)
        self.assertEqual(len(result), COMMANDS_PER_CHUNK * 3)

    def test_parse_stream_trailing_bytes_ignored(self):
        chunk = self._make_chunk([])
        data = chunk + b'\xFF' * 100  # garbage trailing data
        result = parse_stream(data)
        self.assertEqual(len(result), COMMANDS_PER_CHUNK)

    def test_build_chunk_roundtrip(self):
        """build_chunk -> parse_chunk should recover original commands."""
        cmds = [
            JCZCommand(JCZOp.TRAVEL, 0x1000, 0x2000),
            JCZCommand(JCZOp.MARK,   0x3000, 0x4000),
            JCZCommand(JCZOp.MARK,   0x5000, 0x6000),
        ]
        chunk = build_chunk(cmds)
        self.assertEqual(len(chunk), CHUNK_SIZE)
        parsed = parse_chunk(chunk)
        self.assertEqual(parsed[0].opcode, JCZOp.TRAVEL)
        self.assertEqual(parsed[0].p1, 0x1000)
        self.assertEqual(parsed[1].opcode, JCZOp.MARK)
        self.assertEqual(parsed[2].opcode, JCZOp.MARK)
        self.assertTrue(parsed[3].is_nop)

    def test_build_chunk_too_many_commands(self):
        cmds = [JCZCommand(JCZOp.NOP)] * 257
        with self.assertRaises(ValueError):
            build_chunk(cmds)


class TestCoordinateConversion(unittest.TestCase):
    """Galvo <-> mm coordinate conversion tests."""

    def test_centre_is_zero(self):
        """Galvo centre (0x8000, 0x8000) maps to (0.0, 0.0) mm."""
        x, y = galvo_to_mm(0x8000, 0x8000)
        self.assertAlmostEqual(x, 0.0, places=3)
        self.assertAlmostEqual(y, 0.0, places=3)

    def test_origin_is_negative_half_field(self):
        """Galvo (0, 0) maps to approximately (-field/2, -field/2) mm."""
        x, y = galvo_to_mm(0, 0, field_size_mm=110.0)
        self.assertAlmostEqual(x, -55.0, delta=0.01)
        self.assertAlmostEqual(y, -55.0, delta=0.01)

    def test_max_is_positive_half_field(self):
        """Galvo (0xFFFF, 0xFFFF) maps to approximately (+field/2, +field/2) mm."""
        x, y = galvo_to_mm(0xFFFF, 0xFFFF, field_size_mm=110.0)
        self.assertAlmostEqual(x, 55.0, delta=0.01)
        self.assertAlmostEqual(y, 55.0, delta=0.01)

    def test_roundtrip(self):
        """galvo_to_mm -> mm_to_galvo should recover original values (within 1 unit)."""
        gx, gy = 0x6000, 0xA000
        mm_x, mm_y = galvo_to_mm(gx, gy)
        gx2, gy2 = mm_to_galvo(mm_x, mm_y)
        self.assertAlmostEqual(gx, gx2, delta=1)
        self.assertAlmostEqual(gy, gy2, delta=1)

    def test_clamping(self):
        """mm_to_galvo clamps out-of-range values."""
        gx, gy = mm_to_galvo(999.0, -999.0, field_size_mm=110.0)
        self.assertEqual(gx, 0xFFFF)
        self.assertEqual(gy, 0)

    def test_different_field_sizes(self):
        """Different field sizes produce different mm values for same galvo coords."""
        x70, _  = galvo_to_mm(0xC000, 0x8000, field_size_mm=70.0)
        x200, _ = galvo_to_mm(0xC000, 0x8000, field_size_mm=200.0)
        self.assertAlmostEqual(x70 / x200, 70.0 / 200.0, places=3)

    def test_symmetry(self):
        """Equidistant galvo values above/below centre produce equal +/- mm."""
        x_pos, _ = galvo_to_mm(0x8000 + 1000, 0x8000)
        x_neg, _ = galvo_to_mm(0x8000 - 1000, 0x8000)
        self.assertAlmostEqual(x_pos, -x_neg, places=6)


class TestJobSimulation(unittest.TestCase):
    """Simulates a complete JCZ job command stream and verifies parsing."""

    def test_square_job(self):
        """Simulate LightBurn sending a 10mm square at field centre."""
        field = 110.0
        # Convert 10mm square corners to galvo units
        corners_mm = [(-5, -5), (5, -5), (5, 5), (-5, 5), (-5, -5)]
        corners_galvo = [mm_to_galvo(x, y, field) for x, y in corners_mm]

        commands = []
        commands.append(JCZCommand(JCZOp.JOB_BEGIN))
        commands.append(JCZCommand(JCZOp.SET_POWER, p1=2048))  # ~50%
        commands.append(JCZCommand(JCZOp.SET_MARK_SPEED, p1=128))
        commands.append(JCZCommand(JCZOp.LASER_SWITCH, p1=1))

        # Travel to first corner
        gx, gy = corners_galvo[0]
        commands.append(JCZCommand(JCZOp.TRAVEL, p1=gy, p2=gx))

        # Mark to remaining corners
        for gx, gy in corners_galvo[1:]:
            commands.append(JCZCommand(JCZOp.MARK, p1=gy, p2=gx))

        commands.append(JCZCommand(JCZOp.LASER_SWITCH, p1=0))
        commands.append(JCZCommand(JCZOp.JOB_END))

        # Build chunk and parse back
        chunk = build_chunk(commands)
        parsed = parse_chunk(chunk)

        # Extract just the movement commands
        moves = [c for c in parsed if c.is_movement]
        self.assertEqual(len(moves), 5)  # 1 travel + 4 marks
        self.assertTrue(moves[0].is_travel)
        for m in moves[1:]:
            self.assertTrue(m.is_mark)

        # Verify coordinates round-trip back to ~10mm square
        extracted_mm = []
        for m in moves:
            gx, gy = m.xy_galvo
            mx, my = galvo_to_mm(gx, gy, field)
            extracted_mm.append((round(mx, 1), round(my, 1)))

        self.assertEqual(extracted_mm[0], (-5.0, -5.0))
        self.assertEqual(extracted_mm[1], (5.0, -5.0))
        self.assertEqual(extracted_mm[2], (5.0, 5.0))
        self.assertEqual(extracted_mm[3], (-5.0, 5.0))
        self.assertEqual(extracted_mm[4], (-5.0, -5.0))


if __name__ == '__main__':
    unittest.main()
