#!/usr/bin/env python3
"""Unit tests for d1ultra_protocol.py — D1 Ultra binary protocol library."""

import struct
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from d1ultra_protocol import (
    crc16_modbus, make_preview_png, PacketBuilder, ResponseParser,
    Cmd, LaserSource, Peripheral, MAGIC, TERMINATOR, MIN_PACKET,
)


class TestCRC16Modbus(unittest.TestCase):
    """CRC-16/MODBUS implementation tests."""

    def test_empty(self):
        self.assertEqual(crc16_modbus(b''), 0xFFFF)

    def test_known_value(self):
        # CRC-16/MODBUS of "123456789" is 0x4B37
        self.assertEqual(crc16_modbus(b'123456789'), 0x4B37)

    def test_single_byte(self):
        result = crc16_modbus(b'\x00')
        self.assertIsInstance(result, int)
        self.assertTrue(0 <= result <= 0xFFFF)

    def test_deterministic(self):
        data = b'\xDE\xAD\xBE\xEF'
        self.assertEqual(crc16_modbus(data), crc16_modbus(data))


class TestPacketBuilder(unittest.TestCase):
    """Packet builder tests — verifies packet structure and framing."""

    def setUp(self):
        self.builder = PacketBuilder()

    def test_packet_framing(self):
        """Every packet starts with 0x0A0A and ends with 0x0D0D."""
        pkt = self.builder.status()
        self.assertEqual(pkt[:2], MAGIC)
        self.assertEqual(pkt[-2:], TERMINATOR)

    def test_packet_length_field(self):
        """Length field matches actual packet size."""
        pkt = self.builder.status()
        declared_len = struct.unpack('<H', pkt[2:4])[0]
        self.assertEqual(declared_len, len(pkt))

    def test_minimum_size(self):
        """Empty-payload packets are exactly 18 bytes."""
        pkt = self.builder.status()
        self.assertEqual(len(pkt), MIN_PACKET)

    def test_sequence_increments(self):
        """Each packet gets an incrementing sequence number."""
        pkt1 = self.builder.status()
        pkt2 = self.builder.status()
        seq1 = struct.unpack('<H', pkt1[6:8])[0]
        seq2 = struct.unpack('<H', pkt2[6:8])[0]
        self.assertEqual(seq2, seq1 + 1)

    def test_reset_seq(self):
        """reset_seq() starts the counter over."""
        self.builder.status()
        self.builder.status()
        self.builder.reset_seq()
        pkt = self.builder.status()
        seq = struct.unpack('<H', pkt[6:8])[0]
        self.assertEqual(seq, 1)

    def test_cmd_field(self):
        """Command ID is placed at offset 12-13."""
        pkt = self.builder.device_id()
        cmd = struct.unpack('<H', pkt[12:14])[0]
        self.assertEqual(cmd, Cmd.DEVICE_ID)

    def test_msg_type_default(self):
        """Most commands use msg_type=1."""
        pkt = self.builder.status()
        msg_type = struct.unpack('<H', pkt[10:12])[0]
        self.assertEqual(msg_type, 1)

    def test_job_settings_msg_type_zero(self):
        """Job settings use msg_type=0."""
        pkt = self.builder.job_settings(1, 500.0, 50.0, 0.5)
        msg_type = struct.unpack('<H', pkt[10:12])[0]
        self.assertEqual(msg_type, 0)

    def test_path_data_msg_type_zero(self):
        """Path data uses msg_type=0."""
        pkt = self.builder.path_data([(0.0, 0.0), (10.0, 10.0)])
        msg_type = struct.unpack('<H', pkt[10:12])[0]
        self.assertEqual(msg_type, 0)

    def test_crc_validates(self):
        """CRC in packet matches recalculated CRC."""
        pkt = self.builder.job_settings(1, 500.0, 50.0, 0.5)
        pkt_len = struct.unpack('<H', pkt[2:4])[0]
        crc_stored = struct.unpack('<H', pkt[pkt_len - 4 : pkt_len - 2])[0]
        crc_calc = crc16_modbus(pkt[2 : pkt_len - 4])
        self.assertEqual(crc_stored, crc_calc)

    def test_job_settings_payload(self):
        """Job settings payload encodes parameters correctly."""
        pkt = self.builder.job_settings(
            passes=2, speed_mm_min=1000.0, frequency_khz=20.0,
            power_frac=0.75, laser_source=LaserSource.IR)
        payload = pkt[14:-4]  # strip header and CRC+terminator
        self.assertEqual(len(payload), 37)

        passes = struct.unpack('<I', payload[0:4])[0]
        speed  = struct.unpack('<d', payload[4:12])[0]
        freq   = struct.unpack('<d', payload[12:20])[0]
        power  = struct.unpack('<d', payload[20:28])[0]
        source = payload[28]
        unk    = struct.unpack('<d', payload[29:37])[0]

        self.assertEqual(passes, 2)
        self.assertAlmostEqual(speed, 1000.0)
        self.assertAlmostEqual(freq, 20.0)
        self.assertAlmostEqual(power, 0.75)
        self.assertEqual(source, LaserSource.IR)
        self.assertAlmostEqual(unk, -1.0)

    def test_path_data_payload(self):
        """Path data encodes segments correctly: u32 count + (f64 X, f64 Y, 16 zeros)."""
        pts = [(1.5, -2.5), (10.0, 20.0)]
        pkt = self.builder.path_data(pts)
        payload = pkt[14:-4]

        count = struct.unpack('<I', payload[0:4])[0]
        self.assertEqual(count, 2)

        # First segment
        x1 = struct.unpack('<d', payload[4:12])[0]
        y1 = struct.unpack('<d', payload[12:20])[0]
        zeros1 = payload[20:36]
        self.assertAlmostEqual(x1, 1.5)
        self.assertAlmostEqual(y1, -2.5)
        self.assertEqual(zeros1, b'\x00' * 16)

        # Second segment
        x2 = struct.unpack('<d', payload[36:44])[0]
        y2 = struct.unpack('<d', payload[44:52])[0]
        self.assertAlmostEqual(x2, 10.0)
        self.assertAlmostEqual(y2, 20.0)

    def test_workspace_payload(self):
        """Workspace has 42-byte payload: 5 doubles + 2-byte pad."""
        pkt = self.builder.workspace(200.0, -10.0, -15.0, 10.0, 15.0)
        payload = pkt[14:-4]
        self.assertEqual(len(payload), 42)

        speed = struct.unpack('<d', payload[0:8])[0]
        xmin  = struct.unpack('<d', payload[8:16])[0]
        ymin  = struct.unpack('<d', payload[16:24])[0]
        xmax  = struct.unpack('<d', payload[24:32])[0]
        ymax  = struct.unpack('<d', payload[32:40])[0]
        pad   = payload[40:42]

        self.assertAlmostEqual(speed, 200.0)
        self.assertAlmostEqual(xmin, -10.0)
        self.assertAlmostEqual(ymin, -15.0)
        self.assertAlmostEqual(xmax, 10.0)
        self.assertAlmostEqual(ymax, 15.0)
        self.assertEqual(pad, b'\x00\x00')

    def test_job_upload_name_field(self):
        """Job upload pads name to exactly 256 bytes."""
        pkt = self.builder.job_upload("test_job", b'\x89PNG')
        payload = pkt[14:-4]
        name_field = payload[:256]
        self.assertTrue(name_field.startswith(b'test_job\x00'))
        self.assertEqual(len(name_field), 256)

    def test_peripheral_on_off(self):
        """Peripheral control encodes module and state."""
        pkt_on  = self.builder.peripheral(Peripheral.FOCUS_LASER, True)
        pkt_off = self.builder.peripheral(Peripheral.FOCUS_LASER, False)
        payload_on  = pkt_on[14:-4]
        payload_off = pkt_off[14:-4]
        self.assertEqual(payload_on,  bytes([0x02, 0x01]))
        self.assertEqual(payload_off, bytes([0x02, 0x00]))

    def test_ack_reuses_seq(self):
        """build_ack uses the given sequence number, not the auto-increment."""
        ack = self.builder.build_ack(Cmd.QUERY_13, seq=42)
        seq = struct.unpack('<H', ack[6:8])[0]
        self.assertEqual(seq, 42)


class TestResponseParser(unittest.TestCase):
    """Response parser tests with synthetic packets."""

    def _make_packet(self, cmd, payload=b'', msg_type=1, seq=1):
        """Build a valid packet for testing the parser."""
        total_len = 14 + len(payload) + 4
        header = struct.pack('<HH HH HH H',
                             0x0A0A, total_len, 0, seq, 0, msg_type, cmd)
        crc = crc16_modbus(header[2:] + payload)
        return header + payload + struct.pack('<H', crc) + b'\x0d\x0d'

    def test_parse_valid(self):
        pkt = self._make_packet(Cmd.STATUS, seq=5)
        result = ResponseParser.parse_packet(pkt)
        self.assertIsNotNone(result)
        self.assertEqual(result['cmd'], Cmd.STATUS)
        self.assertEqual(result['seq'], 5)

    def test_parse_too_short(self):
        self.assertIsNone(ResponseParser.parse_packet(b'\x0a\x0a'))

    def test_parse_bad_magic(self):
        pkt = self._make_packet(Cmd.STATUS)
        bad = b'\xFF\xFF' + pkt[2:]
        self.assertIsNone(ResponseParser.parse_packet(bad))

    def test_parse_device_name(self):
        # Simulate DEVICE_ID response: u16 status(0) + "D1 Ultra\x00..."
        name = b'D1 Ultra\x00'
        payload = b'\x00\x00' + name + b'\x00' * (30 - len(name))
        pkt = self._make_packet(Cmd.DEVICE_ID, payload)
        result = ResponseParser.parse_packet(pkt)
        self.assertEqual(ResponseParser.parse_device_name(result), "D1 Ultra")

    def test_parse_fw_version(self):
        version = b'1.2.260303.101331'
        payload = b'\x00\x00' + struct.pack('<I', len(version)) + version
        pkt = self._make_packet(Cmd.FW_VERSION, payload)
        result = ResponseParser.parse_packet(pkt)
        self.assertEqual(ResponseParser.parse_fw_version(result), '1.2.260303.101331')

    def test_parse_status_idle(self):
        payload = b'\x00\x00\x00\x00\x00\x00'
        pkt = self._make_packet(Cmd.STATUS, payload)
        result = ResponseParser.parse_packet(pkt)
        self.assertEqual(ResponseParser.parse_status_state(result), 0)

    def test_parse_status_busy(self):
        payload = b'\x00\x00\x00\x00\x01\x00'
        pkt = self._make_packet(Cmd.STATUS, payload)
        result = ResponseParser.parse_packet(pkt)
        self.assertEqual(ResponseParser.parse_status_state(result), 1)


class TestPreviewPNG(unittest.TestCase):
    """PNG preview generator tests."""

    def test_valid_png_signature(self):
        png = make_preview_png()
        self.assertTrue(png.startswith(b'\x89PNG\r\n\x1a\n'))

    def test_reasonable_size(self):
        png = make_preview_png(44, 44)
        self.assertTrue(4000 < len(png) < 10000,
                        f"PNG size {len(png)} outside expected range")

    def test_deterministic(self):
        self.assertEqual(make_preview_png(10, 10), make_preview_png(10, 10))


if __name__ == '__main__':
    unittest.main()
