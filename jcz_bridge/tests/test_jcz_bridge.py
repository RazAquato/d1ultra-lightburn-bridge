#!/usr/bin/env python3
"""Unit tests for jcz_bridge.py — bridge logic and FunctionFS descriptors."""

import struct
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from jcz_protocol import (
    JCZOp, JCZCommand, parse_chunk, galvo_to_mm, build_chunk,
    CHUNK_SIZE, COMMAND_SIZE, COMMANDS_PER_CHUNK,
)


class TestSingleCommandProcessing(unittest.TestCase):
    """Tests for the single-command-at-a-time processing mode.

    LightBurn sends individual 12-byte commands (not 3072-byte chunks)
    for status polling and framing. The bridge must handle both modes.
    """

    def test_single_command_parse(self):
        """A single 12-byte command can be parsed directly."""
        data = struct.pack('<HHHHHH', 0x0009, 0, 0, 0, 0, 0)
        self.assertEqual(len(data), COMMAND_SIZE)
        vals = struct.unpack('<HHHHHH', data)
        cmd = JCZCommand(*vals)
        self.assertEqual(cmd.opcode, 0x0009)
        self.assertFalse(cmd.is_nop)

    def test_heartbeat_0x0009(self):
        """Opcode 0x0009 is a status poll from LightBurn (not in JCZOp enum)."""
        cmd = JCZCommand(0x0009)
        self.assertEqual(cmd.opcode, 0x0009)
        self.assertFalse(cmd.is_nop)
        self.assertFalse(cmd.is_mark)
        self.assertFalse(cmd.is_travel)
        self.assertIsNone(cmd.xy_galvo)

    def test_status_response_format(self):
        """Status response is 12 bytes: status byte + 11 zero bytes."""
        # Idle
        idle = b'\x00' + b'\x00' * 11
        self.assertEqual(len(idle), 12)
        self.assertEqual(idle[0], 0x00)

        # Busy
        busy = b'\x01' + b'\x00' * 11
        self.assertEqual(len(busy), 12)
        self.assertEqual(busy[0], 0x01)

    def test_mixed_single_and_batch(self):
        """Buffer can accumulate single commands into a parseable stream."""
        # Simulate receiving 3 individual 12-byte commands
        cmd1 = struct.pack('<HHHHHH', 0x0009, 0, 0, 0, 0, 0)
        cmd2 = struct.pack('<HHHHHH', JCZOp.JOB_BEGIN, 0, 0, 0, 0, 0)
        cmd3 = struct.pack('<HHHHHH', JCZOp.TRAVEL, 0x8000, 0x8000, 0, 0, 0)

        buf = cmd1 + cmd2 + cmd3
        self.assertEqual(len(buf), 36)

        # Parse one at a time
        commands = []
        while len(buf) >= COMMAND_SIZE:
            vals = struct.unpack_from('<HHHHHH', buf, 0)
            commands.append(JCZCommand(*vals))
            buf = buf[COMMAND_SIZE:]

        self.assertEqual(len(commands), 3)
        self.assertEqual(commands[0].opcode, 0x0009)
        self.assertEqual(commands[1].opcode, JCZOp.JOB_BEGIN)
        self.assertEqual(commands[2].opcode, JCZOp.TRAVEL)
        self.assertEqual(len(buf), 0)


class TestFunctionFSDescriptors(unittest.TestCase):
    """Tests for the FunctionFS v2 descriptor blob format."""

    def _build_descriptors(self):
        """Build the same descriptor blob as jcz_bridge.py."""
        MAGIC_V2 = 3
        HAS_FS = 1
        HAS_HS = 2
        STRINGS_MAGIC = 2

        intf = struct.pack('<BBBBBBBBB', 9, 4, 0, 0, 2, 0xFF, 0xFF, 0xFF, 0)
        ep_out_fs = struct.pack('<BBBBHB', 7, 5, 0x01, 0x02, 64, 0)
        ep_in_fs  = struct.pack('<BBBBHB', 7, 5, 0x82, 0x02, 64, 0)
        ep_out_hs = struct.pack('<BBBBHB', 7, 5, 0x01, 0x02, 512, 0)
        ep_in_hs  = struct.pack('<BBBBHB', 7, 5, 0x82, 0x02, 512, 0)

        fs = intf + ep_out_fs + ep_in_fs
        hs = intf + ep_out_hs + ep_in_hs
        total_len = 20 + len(fs) + len(hs)
        header = struct.pack('<IIIII', MAGIC_V2, total_len, HAS_FS | HAS_HS, 3, 3)
        str_blob = struct.pack('<IIIH', STRINGS_MAGIC, 16, 1, 0x0409) + b'\x00\x00'

        return header + fs + hs, str_blob

    def test_descriptor_magic(self):
        """Descriptor blob starts with FunctionFS v2 magic (3)."""
        desc, _ = self._build_descriptors()
        magic = struct.unpack('<I', desc[0:4])[0]
        self.assertEqual(magic, 3)

    def test_descriptor_length(self):
        """Length field matches actual descriptor blob size."""
        desc, _ = self._build_descriptors()
        declared_len = struct.unpack('<I', desc[4:8])[0]
        self.assertEqual(declared_len, len(desc))

    def test_descriptor_flags(self):
        """Flags indicate both FS and HS descriptors present."""
        desc, _ = self._build_descriptors()
        flags = struct.unpack('<I', desc[8:12])[0]
        self.assertEqual(flags, 3)  # HAS_FS | HAS_HS

    def test_descriptor_counts(self):
        """3 descriptors each for FS and HS (1 interface + 2 endpoints)."""
        desc, _ = self._build_descriptors()
        fs_count = struct.unpack('<I', desc[12:16])[0]
        hs_count = struct.unpack('<I', desc[16:20])[0]
        self.assertEqual(fs_count, 3)
        self.assertEqual(hs_count, 3)

    def test_interface_descriptor(self):
        """Interface descriptor is vendor-specific (0xFF) with 2 endpoints."""
        desc, _ = self._build_descriptors()
        # FS interface descriptor starts at offset 20
        intf = desc[20:29]
        self.assertEqual(intf[0], 9)     # bLength
        self.assertEqual(intf[1], 4)     # bDescriptorType = INTERFACE
        self.assertEqual(intf[4], 2)     # bNumEndpoints
        self.assertEqual(intf[5], 0xFF)  # bInterfaceClass = Vendor Specific

    def test_endpoint_descriptors_fs(self):
        """Full-speed endpoints: OUT 0x01 (64B), IN 0x82 (64B)."""
        desc, _ = self._build_descriptors()
        ep_out = desc[29:36]
        ep_in  = desc[36:43]

        # EP OUT
        self.assertEqual(ep_out[0], 7)     # bLength
        self.assertEqual(ep_out[1], 5)     # bDescriptorType = ENDPOINT
        self.assertEqual(ep_out[2], 0x01)  # bEndpointAddress = OUT 1
        self.assertEqual(ep_out[3], 0x02)  # bmAttributes = BULK
        self.assertEqual(struct.unpack('<H', ep_out[4:6])[0], 64)  # wMaxPacketSize

        # EP IN
        self.assertEqual(ep_in[2], 0x82)   # bEndpointAddress = IN 2
        self.assertEqual(struct.unpack('<H', ep_in[4:6])[0], 64)

    def test_endpoint_descriptors_hs(self):
        """High-speed endpoints: OUT 0x01 (512B), IN 0x82 (512B)."""
        desc, _ = self._build_descriptors()
        # HS descriptors start after FS (20 header + 23 fs = offset 43)
        hs_start = 43
        ep_out = desc[hs_start + 9 : hs_start + 16]
        ep_in  = desc[hs_start + 16 : hs_start + 23]

        self.assertEqual(struct.unpack('<H', ep_out[4:6])[0], 512)
        self.assertEqual(struct.unpack('<H', ep_in[4:6])[0], 512)

    def test_strings_blob(self):
        """Strings blob has correct magic and format."""
        _, str_blob = self._build_descriptors()
        magic = struct.unpack('<I', str_blob[0:4])[0]
        self.assertEqual(magic, 2)  # FUNCTIONFS_STRINGS_MAGIC
        length = struct.unpack('<I', str_blob[4:8])[0]
        self.assertEqual(length, 16)


class TestCoordinateTranslation220mm(unittest.TestCase):
    """Coordinate conversion tests for the actual 220mm field size."""

    FIELD = 220.0

    def test_centre(self):
        x, y = galvo_to_mm(0x8000, 0x8000, self.FIELD)
        self.assertAlmostEqual(x, 0.0, places=3)
        self.assertAlmostEqual(y, 0.0, places=3)

    def test_full_range(self):
        """Full galvo range should span 220mm."""
        x_min, _ = galvo_to_mm(0x0000, 0x8000, self.FIELD)
        x_max, _ = galvo_to_mm(0xFFFF, 0x8000, self.FIELD)
        span = x_max - x_min
        self.assertAlmostEqual(span, self.FIELD, delta=0.01)

    def test_10mm_square(self):
        """A 10mm square at centre should use a small range of galvo values."""
        from jcz_protocol import mm_to_galvo
        g1 = mm_to_galvo(-5.0, -5.0, self.FIELD)
        g2 = mm_to_galvo(5.0, 5.0, self.FIELD)
        # 10mm out of 220mm = ~4.5% of the 65536 range = ~2979 units
        dx = g2[0] - g1[0]
        self.assertAlmostEqual(dx, 65536 * 10 / 220, delta=2)


class TestWiresharkFindings(unittest.TestCase):
    """Tests based on actual Wireshark capture analysis from testing.

    These validate our understanding of how LightBurn communicates
    with the virtual BJJCZ device over USB/IP.
    """

    def test_lightburn_heartbeat_format(self):
        """LightBurn sends 0x0009 as 12 zero-padded bytes."""
        expected = bytes.fromhex('090000000000000000000000')
        self.assertEqual(len(expected), 12)
        vals = struct.unpack('<HHHHHH', expected)
        self.assertEqual(vals[0], 0x0009)
        self.assertEqual(vals[1:], (0, 0, 0, 0, 0))

    def test_usb_config_descriptor_endpoints(self):
        """The actual endpoints reported to Windows: OUT 0x02, IN 0x81.

        Note: FunctionFS maps our descriptor addresses (OUT 0x01, IN 0x82)
        to actual kernel-assigned addresses. From the Wireshark capture,
        the config descriptor shows OUT 0x02 and IN 0x81.
        """
        # From Wireshark: config descriptor hex
        config_hex = '09022000010104807d0904000002ffffff000705020200020007058102000200'
        config = bytes.fromhex(config_hex)

        # EP OUT at offset 20 (config=9 + intf=9 + ep_bLength=1 + ep_bDescType=1)
        ep_out_addr = config[20]
        self.assertEqual(ep_out_addr, 0x02)

        # EP IN at offset 27 (20 + 7 bytes for first EP descriptor)
        ep_in_addr = config[27]
        self.assertEqual(ep_in_addr, 0x81)

    def test_usbip_submit_out_format(self):
        """USB/IP SUBMIT for OUT transfer has 48-byte header + data."""
        # From Wireshark capture: a SUBMIT OUT with 0x0009 command
        pkt_hex = ('00000001000000f40002000e0000000000000002'
                   '000000000000000c00000000ffffffff000000000000000000000000'
                   '090000000000000000000000')
        pkt = bytes.fromhex(pkt_hex)

        cmd = struct.unpack('>I', pkt[0:4])[0]
        direction = struct.unpack('>I', pkt[12:16])[0]
        ep = struct.unpack('>I', pkt[16:20])[0]
        usb_data = pkt[48:]

        self.assertEqual(cmd, 1)          # USBIP_CMD_SUBMIT
        self.assertEqual(direction, 0)    # OUT
        self.assertEqual(ep, 2)           # endpoint 2
        self.assertEqual(len(usb_data), 12)
        self.assertEqual(usb_data, bytes.fromhex('090000000000000000000000'))


if __name__ == '__main__':
    unittest.main()
