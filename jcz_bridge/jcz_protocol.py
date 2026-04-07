#!/usr/bin/env python3
"""
JCZ/BJJCZ Protocol — Command Parser & Coordinate Conversion
=============================================================

Parses the 12-byte binary commands sent by LightBurn (and EzCad2) to BJJCZ
galvo laser controller boards. This module is **standalone** — it has no
dependencies on the D1 Ultra protocol or the bridge logic.

Protocol overview (from Bryce Schroeder's balor reverse engineering):
    - USB: Bulk OUT 0x02 (commands from host), Bulk IN 0x88 (status to host)
    - Commands: exactly 12 bytes each = u16 opcode + 5x u16 params (little-endian)
    - Chunking: sent in batches of 256 commands = 3072 bytes, padded with NOPs
    - Coordinates: 16-bit unsigned (0x0000-0xFFFF), centre = 0x8000

References:
    - balor: https://gitlab.com/bryce15/balor
    - galvoplotter: https://github.com/meerk40t/galvoplotter

License: MIT
"""

import struct
from enum import IntEnum
from typing import List, Tuple, Optional

__all__ = [
    "JCZOp", "JCZCommand", "parse_chunk", "parse_stream",
    "galvo_to_mm", "mm_to_galvo", "DEFAULT_FIELD_SIZE_MM",
]

# ═══════════════════════════════════════════════════════════════════════════════
# JCZ opcodes
# ═══════════════════════════════════════════════════════════════════════════════

class JCZOp(IntEnum):
    """BJJCZ command opcodes (subset relevant to laser marking).

    Each command is 12 bytes: u16 opcode followed by five u16 parameters.
    For movement commands (TRAVEL, MARK): param1=Y, param2=X (note the order).
    """

    # --- Movement ---
    TRAVEL          = 0x8001   # rapid move, laser off:  p1=Y, p2=X
    NOP             = 0x8002   # no-op / padding
    MARK            = 0x8005   # marking move, laser on: p1=Y, p2=X

    # --- Laser on/off (same opcode, different param) ---
    LASER_SWITCH    = 0x8021   # p1=1 -> on, p1=0 -> off

    # --- Timing / delays ---
    TRAVEL_DELAY    = 0x8003   # delay after travel
    MARK_END_DELAY  = 0x8004   # delay after mark segment
    POLYGON_DELAY   = 0x8006   # delay at polygon corners
    LASER_ON_DELAY  = 0x8007   # delay after laser on
    LASER_OFF_DELAY = 0x8008   # delay after laser off

    # --- Speed ---
    SET_MARK_SPEED   = 0x800C  # set marking speed
    SET_TRAVEL_SPEED = 0x800D  # set travel/jump speed

    # --- Power / frequency ---
    SET_POWER        = 0x8012  # set laser power (0-4095)
    SET_Q_PERIOD     = 0x801B  # set Q-switch period (frequency)
    SET_Q_PULSE      = 0x801C  # set Q-switch pulse width

    # --- Job control ---
    JOB_BEGIN   = 0x8051  # begin job execution
    JOB_END     = 0x8052  # end job

    # --- System ---
    WRITE_PORT  = 0x8011  # write to I/O port
    RESET       = 0x8050  # reset controller


# Opcodes that carry X/Y coordinates
_MOVEMENT_OPS = frozenset({JCZOp.TRAVEL, JCZOp.MARK})

# How many commands per chunk
COMMANDS_PER_CHUNK = 256
COMMAND_SIZE       = 12
CHUNK_SIZE         = COMMANDS_PER_CHUNK * COMMAND_SIZE  # 3072 bytes


# ═══════════════════════════════════════════════════════════════════════════════
# JCZ command
# ═══════════════════════════════════════════════════════════════════════════════

class JCZCommand:
    """A single 12-byte JCZ/BJJCZ command.

    Attributes:
        opcode: u16 command opcode (see JCZOp enum).
        p1-p5:  u16 parameters. For TRAVEL/MARK: p1=Y, p2=X (galvo coords).
    """
    __slots__ = ('opcode', 'p1', 'p2', 'p3', 'p4', 'p5')

    def __init__(self, opcode: int, p1: int = 0, p2: int = 0,
                 p3: int = 0, p4: int = 0, p5: int = 0):
        self.opcode = opcode
        self.p1 = p1
        self.p2 = p2
        self.p3 = p3
        self.p4 = p4
        self.p5 = p5

    def __repr__(self) -> str:
        try:
            name = JCZOp(self.opcode).name
        except ValueError:
            name = f'0x{self.opcode:04x}'
        return (f'JCZCommand({name}, '
                f'p1=0x{self.p1:04x}, p2=0x{self.p2:04x}, '
                f'p3=0x{self.p3:04x}, p4=0x{self.p4:04x}, p5=0x{self.p5:04x})')

    def to_bytes(self) -> bytes:
        """Serialize back to 12 bytes (little-endian)."""
        return struct.pack('<HHHHHH',
                           self.opcode, self.p1, self.p2, self.p3, self.p4, self.p5)

    @property
    def is_nop(self) -> bool:
        return self.opcode == JCZOp.NOP

    @property
    def is_travel(self) -> bool:
        return self.opcode == JCZOp.TRAVEL

    @property
    def is_mark(self) -> bool:
        return self.opcode == JCZOp.MARK

    @property
    def is_movement(self) -> bool:
        return self.opcode in _MOVEMENT_OPS

    @property
    def xy_galvo(self) -> Optional[Tuple[int, int]]:
        """Return (x, y) in galvo units, or None if not a movement command.

        Note: JCZ stores Y in p1, X in p2 — this property returns (X, Y).
        """
        if self.opcode in _MOVEMENT_OPS:
            return (self.p2, self.p1)  # swap: p1=Y, p2=X -> (X, Y)
        return None

    @property
    def is_laser_on(self) -> Optional[bool]:
        """True if LASER_SWITCH with state=on, False if off, None if not a switch cmd."""
        if self.opcode == JCZOp.LASER_SWITCH:
            return self.p1 != 0
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# Chunk / stream parsing
# ═══════════════════════════════════════════════════════════════════════════════

def parse_chunk(data: bytes) -> List[JCZCommand]:
    """Parse a 3072-byte chunk into 256 JCZ commands.

    Raises ValueError if data is not exactly 3072 bytes.
    """
    if len(data) != CHUNK_SIZE:
        raise ValueError(f"Expected {CHUNK_SIZE} bytes, got {len(data)}")
    commands = []
    for i in range(COMMANDS_PER_CHUNK):
        offset = i * COMMAND_SIZE
        vals = struct.unpack_from('<HHHHHH', data, offset)
        commands.append(JCZCommand(*vals))
    return commands


def parse_stream(data: bytes) -> List[JCZCommand]:
    """Parse a byte stream that may contain multiple 3072-byte chunks.

    Silently skips incomplete trailing chunks.
    """
    commands = []
    for offset in range(0, len(data) - (CHUNK_SIZE - 1), CHUNK_SIZE):
        chunk = data[offset : offset + CHUNK_SIZE]
        if len(chunk) == CHUNK_SIZE:
            commands.extend(parse_chunk(chunk))
    return commands


def build_chunk(commands: List[JCZCommand]) -> bytes:
    """Serialize a list of commands into a 3072-byte chunk, NOP-padded.

    Raises ValueError if more than 256 commands are provided.
    """
    if len(commands) > COMMANDS_PER_CHUNK:
        raise ValueError(f"Too many commands: {len(commands)} > {COMMANDS_PER_CHUNK}")
    data = bytearray()
    for cmd in commands:
        data += cmd.to_bytes()
    # Pad with NOPs
    nop = JCZCommand(JCZOp.NOP).to_bytes()
    while len(data) < CHUNK_SIZE:
        data += nop
    return bytes(data)


# ═══════════════════════════════════════════════════════════════════════════════
# Coordinate conversion
# ═══════════════════════════════════════════════════════════════════════════════

DEFAULT_FIELD_SIZE_MM = 110.0

# JCZ galvo coordinates: 0x0000 to 0xFFFF (65535)
# Centre of field = 0x8000 (32768)
# Physical mapping: galvo_units * (field_size_mm / 65536) = mm from centre


def galvo_to_mm(galvo_x: int, galvo_y: int,
                field_size_mm: float = DEFAULT_FIELD_SIZE_MM) -> Tuple[float, float]:
    """Convert JCZ galvo coordinates (0-65535) to mm relative to field centre.

    Returns (mm_x, mm_y) where (0, 0) is the centre of the galvo field.
    """
    scale = field_size_mm / 65536.0
    mm_x = (galvo_x - 0x8000) * scale
    mm_y = (galvo_y - 0x8000) * scale
    return (mm_x, mm_y)


def mm_to_galvo(mm_x: float, mm_y: float,
                field_size_mm: float = DEFAULT_FIELD_SIZE_MM) -> Tuple[int, int]:
    """Convert mm (relative to field centre) back to JCZ galvo coordinates.

    Clamps result to 0-65535.
    """
    scale = 65536.0 / field_size_mm
    gx = int(0x8000 + mm_x * scale)
    gy = int(0x8000 + mm_y * scale)
    return (max(0, min(0xFFFF, gx)), max(0, min(0xFFFF, gy)))
