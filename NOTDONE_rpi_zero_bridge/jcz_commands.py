"""
JCZ/BJJCZ command definitions and parser.

Based on Bryce Schroeder's balor reverse engineering of the BJJCZ LMC controller.
See: https://gitlab.com/bryce15/balor

Every JCZ command is exactly 12 bytes: u16 opcode + 5x u16 parameters.
Commands are sent in chunks of 256 (3072 bytes total). Incomplete chunks
are padded with NOP (0x8002) commands.
"""

import struct
from typing import List, Tuple, Optional
from enum import IntEnum

# ─────────────────────────────────────────────────────────────────────────────
# JCZ opcodes (subset — the ones relevant for laser marking)
# ─────────────────────────────────────────────────────────────────────────────

class JCZOp(IntEnum):
    # Movement
    TRAVEL      = 0x8001  # Rapid move (laser off): params = Y, X, angle, distance, ?
    MARK        = 0x8005  # Cut/mark move (laser on): params = Y, X, angle, distance, ?
    NOP         = 0x8002  # No-op (padding)

    # Laser control
    LASER_ON    = 0x8021  # Laser on: param1 = 1
    LASER_OFF   = 0x8021  # Laser off: param1 = 0 (same opcode, different param)

    # Timing / delays
    MARK_END_DELAY    = 0x8004  # Delay after mark segment
    TRAVEL_DELAY      = 0x8003  # Delay after travel
    POLYGON_DELAY     = 0x8006  # Delay at polygon corners
    LASER_ON_DELAY    = 0x8007  # Delay after laser on
    LASER_OFF_DELAY   = 0x8008  # Delay after laser off

    # Speed
    SET_MARK_SPEED    = 0x800C  # Set marking speed
    SET_TRAVEL_SPEED  = 0x800D  # Set travel/jump speed

    # Power / frequency
    SET_POWER         = 0x8012  # Set laser power (0-4095)
    SET_Q_PERIOD      = 0x801B  # Set Q-switch period (frequency)
    SET_Q_PULSE_WIDTH = 0x801C  # Set Q-switch pulse width

    # Job control
    JOB_BEGIN   = 0x8051  # Begin job execution
    JOB_END     = 0x8052  # End job

    # System
    WRITE_PORT  = 0x8011  # Write to I/O port (peripherals)
    RESET       = 0x8050  # Reset controller


# ─────────────────────────────────────────────────────────────────────────────
# Command parsing
# ─────────────────────────────────────────────────────────────────────────────

class JCZCommand:
    """A single 12-byte JCZ command."""
    __slots__ = ('opcode', 'p1', 'p2', 'p3', 'p4', 'p5')

    def __init__(self, opcode: int, p1: int = 0, p2: int = 0,
                 p3: int = 0, p4: int = 0, p5: int = 0):
        self.opcode = opcode
        self.p1 = p1
        self.p2 = p2
        self.p3 = p3
        self.p4 = p4
        self.p5 = p5

    def __repr__(self):
        name = JCZOp(self.opcode).name if self.opcode in JCZOp.__members__.values() else f'0x{self.opcode:04x}'
        return f'JCZCommand({name}, {self.p1}, {self.p2}, {self.p3}, {self.p4}, {self.p5})'

    @property
    def is_travel(self) -> bool:
        return self.opcode == JCZOp.TRAVEL

    @property
    def is_mark(self) -> bool:
        return self.opcode == JCZOp.MARK

    @property
    def is_nop(self) -> bool:
        return self.opcode == JCZOp.NOP

    @property
    def xy(self) -> Optional[Tuple[int, int]]:
        """Return (x, y) in galvo units if this is a travel or mark command."""
        if self.opcode in (JCZOp.TRAVEL, JCZOp.MARK):
            return (self.p2, self.p1)  # p1=Y, p2=X in JCZ protocol
        return None


def parse_chunk(data: bytes) -> List[JCZCommand]:
    """Parse a 3072-byte chunk into 256 JCZ commands."""
    if len(data) != 3072:
        raise ValueError(f"Expected 3072 bytes, got {len(data)}")
    commands = []
    for i in range(256):
        offset = i * 12
        vals = struct.unpack_from('<HHHHHH', data, offset)
        commands.append(JCZCommand(*vals))
    return commands


def parse_stream(data: bytes) -> List[JCZCommand]:
    """Parse a stream of JCZ data (may contain multiple chunks)."""
    commands = []
    for offset in range(0, len(data) - 11, 3072):
        chunk = data[offset:offset + 3072]
        if len(chunk) == 3072:
            commands.extend(parse_chunk(chunk))
    return commands


# ─────────────────────────────────────────────────────────────────────────────
# Coordinate conversion
# ─────────────────────────────────────────────────────────────────────────────

# JCZ galvo coordinates: 0x0000 to 0xFFFF (65535)
# Center of field = 0x8000 (32768)
# D1 Ultra coordinates: mm, centered around design bounding-box midpoint

# The mapping depends on the galvo's physical calibration.
# For a typical BJJCZ setup with ~110mm field:
#   galvo_center = 32768
#   mm_per_unit = field_size_mm / 65536

DEFAULT_FIELD_SIZE_MM = 110.0  # Typical galvo field size

def galvo_to_mm(galvo_x: int, galvo_y: int,
                field_size_mm: float = DEFAULT_FIELD_SIZE_MM) -> Tuple[float, float]:
    """Convert JCZ galvo coordinates (0-65535) to mm relative to center."""
    center = 32768
    scale = field_size_mm / 65536.0
    mm_x = (galvo_x - center) * scale
    mm_y = (galvo_y - center) * scale
    return (mm_x, mm_y)


def mm_to_galvo(mm_x: float, mm_y: float,
                field_size_mm: float = DEFAULT_FIELD_SIZE_MM) -> Tuple[int, int]:
    """Convert mm (relative to center) to JCZ galvo coordinates (0-65535)."""
    center = 32768
    scale = 65536.0 / field_size_mm
    gx = int(center + mm_x * scale)
    gy = int(center + mm_y * scale)
    return (max(0, min(65535, gx)), max(0, min(65535, gy)))
