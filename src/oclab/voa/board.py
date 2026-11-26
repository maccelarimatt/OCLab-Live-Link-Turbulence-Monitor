"""Serial link to the VOA player sketch (firmware/voa_player/voa_player.ino).

PC to Arduino:
    'R'                   reset; the board replies 'K'
    'V' lo hi             set the output now (12-bit code, little-endian)
    'Q'                   read A0 16 times when idle; reply 'Q', 0x80|(sum>>7), sum&0x7F
    'D' n (lo hi) x n     queue n samples (n <= 64) into the 512-sample ring buffer
    'G'                   start playback at 1 kHz
    'F'                   finish once the buffer is empty

Arduino to PC:
    'C'                   64 samples played: the PC may send 64 more
    0x80|(v>>7), v&0x7F   one A0 sample per tick, read just before each output update,
                          so sample k shows the light produced by output k-1
    'Z' played underruns i2c_errors max_late_us monitor_dropped '\\n'
    'E'                   the sketch is built for the MCP4725 DAC but none was found

Bytes of 0x80 and above only ever start a monitor pair.
"""
from __future__ import annotations

from datetime import datetime, timezone
import struct
import time

import numpy as np

BAUD = 115200
CHUNK = 64
BUFFER = 512
PREFILL_CHUNKS = 7           # 448 samples, inside the 512-sample buffer
BOOT_WAIT_S = 2.5            # Opening the port resets the Uno; stay silent while the bootloader runs
READY_TIMEOUT_S = 4.0
RESET_INTERVAL_S = 0.5
STALL_TIMEOUT_S = 3.0
REPORT_FIELDS = ("played", "underruns", "i2c_errors", "max_late_us", "monitor_dropped")


class WrongSketch(RuntimeError):
    pass


class MonitorDecoder:
    """Separate monitor pairs, credits and the final report in the byte stream.

    feed() may be given the stream split at any point.
    """

    def __init__(self):
        self.monitor: list[int] = []
        self.credits = 0
        self.unexpected = 0
        self.report: dict | None = None
        self._high: int | None = None
        self._line: bytearray | None = None

    def feed(self, data: bytes) -> int:
        """Consume bytes; return the number of new credits."""
        credits = 0
        for byte in data:
            if self._line is not None:
                if byte == 0x0A:
                    fields = self._line.decode("ascii", "replace").split()
                    try:
                        self.report = dict(zip(REPORT_FIELDS, map(int, fields)))
                    except ValueError:
                        raise RuntimeError(f"Malformed end-of-run report {bytes(self._line)!r}") from None
                    self._line = None
                elif byte != 0x0D:
                    self._line.append(byte)
            elif self._high is not None:
                self.monitor.append(((self._high & 0x7F) << 7) | byte)
                self._high = None
            elif byte >= 0x80:
                self._high = byte
            elif byte == ord("C"):
                credits += 1
            elif byte == ord("Z"):
                self._line = bytearray()
            else:
                self.unexpected += 1
        self.credits += credits
        return credits


class Board:
    """The Arduino running voa_player.ino. Every code sent is checked against `limit`."""

    def __init__(self, port: str, limit: int, boot_wait_s: float = BOOT_WAIT_S, link=None):
        if link is None:
            import serial
            link = serial.Serial(port, BAUD, timeout=0.05)
        self.link = link
        self.limit = int(limit)
        self.port = port
        try:
            self._wait_for_board(boot_wait_s)
        except BaseException:
            self.link.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self.link.close()

    def _check(self, codes) -> np.ndarray:
        codes = np.asarray(codes)
        if codes.size == 0:
            raise ValueError("No codes to send")
        if not np.issubdtype(codes.dtype, np.integer):
            raise ValueError("Codes must be integers")
        if codes.min() < 0 or codes.max() > self.limit:
            raise ValueError(f"Code {int(codes.max()) if codes.max() > self.limit else int(codes.min())} is outside "
                             f"0 to {self.limit} (the software voltage limit); nothing was sent")
        return codes.astype("<u2")

    def _wait_for_board(self, boot_wait_s: float):
        received = bytearray()
        end = time.monotonic() + boot_wait_s
        while time.monotonic() < end:
            received += self.link.read(64)
        deadline = time.monotonic() + READY_TIMEOUT_S
        next_reset = 0.0
        while time.monotonic() < deadline:
            if time.monotonic() >= next_reset:
                self.link.write(b"R")
                self.link.flush()
                next_reset = time.monotonic() + RESET_INTERVAL_S
            received += self.link.read(64)
            other = bytes(b for b in received if b not in b"KE")
            if other:
                text = other.decode("ascii", "replace").strip()
                raise WrongSketch(f"The board on {self.port} is running a different sketch: it sent {text[:60]!r}. "
                                  "Upload firmware/voa_player/voa_player.ino.")
            if b"E" in received:
                raise RuntimeError("The sketch is built for the MCP4725 DAC and cannot find it. "
                                   "Set OUTPUT_PWM to 1 for the PWM circuit and upload it again.")
            if b"K" in received:
                time.sleep(0.05)
                self.link.reset_input_buffer()
                self._check_monitor()
                return
        raise TimeoutError(f"No reply from the Arduino on {self.port} after {boot_wait_s + READY_TIMEOUT_S:.0f} s. "
                           "Check that voa_player.ino is uploaded and that no Serial Monitor holds the port.")

    def _check_monitor(self):
        """Version 1 of the streamer answers 'R' but has no A0 monitor; playback logs would be empty."""
        try:
            self.query(1)
        except RuntimeError:
            raise WrongSketch(f"The board on {self.port} answers 'R' but not the monitor query 'Q': it is running "
                              "an older sketch without the A0 monitor (such as voa_dac_streamer version 1). "
                              "Upload firmware/voa_player/voa_player.ino.") from None

    def set_code(self, code: int):
        (value,) = self._check([code])
        self.link.write(b"V" + struct.pack("<H", int(value)))
        self.link.flush()

    def query(self, repeats: int = 4) -> float:
        """Mean A0 code over 16 x repeats conversions (only when not playing)."""
        total = 0
        for _ in range(repeats):
            self.link.write(b"Q")
            self.link.flush()
            reply = bytearray()
            deadline = time.monotonic() + 1.0
            while len(reply) < 3 and time.monotonic() < deadline:
                reply += self.link.read(3 - len(reply))
            if len(reply) != 3 or reply[0:1] != b"Q" or reply[1] < 0x80:
                raise RuntimeError(f"Unexpected monitor reply {bytes(reply)!r}; is voa_player.ino uploaded?")
            total += ((reply[1] & 0x7F) << 7) | reply[2]
        return total / (16.0 * repeats)

    def _send(self, codes: np.ndarray):
        self.link.write(b"D" + bytes([len(codes)]) + codes.tobytes())

    def play(self, codes):
        """Play codes at 1 kHz. Returns (board report, A0 samples, UTC start, UTC end, unexpected bytes)."""
        codes = self._check(codes)
        position = 0
        for _ in range(PREFILL_CHUNKS):
            if position < codes.size:
                self._send(codes[position:position + CHUNK])
                position += CHUNK
        self.link.write(b"G")
        self.link.flush()
        started = datetime.now(timezone.utc)
        decoder = MonitorDecoder()
        finish_sent = False
        last_data = time.monotonic()
        while decoder.report is None:
            if position >= codes.size and not finish_sent:
                self.link.write(b"F")
                self.link.flush()
                finish_sent = True
            data = self.link.read(256)
            if not data:
                if time.monotonic() - last_data > STALL_TIMEOUT_S:
                    raise TimeoutError("Playback stalled; the output holds its last value")
                continue
            last_data = time.monotonic()
            for _ in range(decoder.feed(data)):
                if position < codes.size:
                    self._send(codes[position:position + CHUNK])
                    position += CHUNK
        ended = datetime.now(timezone.utc)
        return decoder.report, np.array(decoder.monitor, dtype=np.uint16), started, ended, decoder.unexpected
