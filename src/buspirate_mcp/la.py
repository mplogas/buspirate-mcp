"""BusPirate 6 Follow Along Logic Analyzer (FALA) session management.

FALA (binmode 4) auto-captures bus activity on all 8 IO pins every time a bus
command runs on the terminal. The capture data arrives on ACM1 as $FALADATA
notifications followed by raw binary samples.

This module is the ONLY module that manages the FALA serial ports.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from buspirate_mcp.la_parsers import parse_fala_notification

try:
    import serial
except ImportError:
    serial = None  # type: ignore

logger = logging.getLogger("buspirate-mcp.la")

_TERMINAL_MODE_NUM = {
    "spi": "6",
    "i2c": "5",
    "uart": "3",
    "1wire": "2",
}

_TERMINAL_PROMPT = {
    "spi": "SPI>",
    "i2c": "I2C>",
    "uart": "UART>",
    "1wire": "1-WIRE>",
    "hiz": "HiZ>",
}


class FALASession:
    """Manages the BP6 FALA dual-port workflow."""

    def __init__(self, terminal_port: str, fala_port: str):
        self._terminal_port = terminal_port
        self._fala_port = fala_port
        self._term = None  # serial.Serial for ACM0
        self._fala = None  # serial.Serial for ACM1
        self._protocol: str | None = None
        self._active = False
        self._last_notification: dict | None = None

    @property
    def active(self) -> bool:
        return self._active

    @property
    def protocol(self) -> str | None:
        return self._protocol

    def activate(self, protocol: str, protocol_config: dict | None = None) -> dict:
        """Switch to FALA binmode, enter bus protocol on terminal."""
        if protocol not in _TERMINAL_MODE_NUM:
            raise ValueError(f"Unsupported protocol: {protocol}")

        # Open terminal port
        self._term = serial.Serial(self._terminal_port, 115200, timeout=2)
        time.sleep(0.5)
        self._term.reset_input_buffer()

        # Dismiss VT100 prompt and get to HiZ>
        self._dismiss_vt100()

        # Switch to FALA binmode (4)
        self._send_and_read(b'binmode\r', delay=1.0)
        resp = self._send_and_read(b'4\r', delay=1.0)
        if "Save" in resp:
            self._send_and_read(b'n\r', delay=1.0)

        # Wait for FALA activation message
        time.sleep(0.5)
        self._term.read(self._term.in_waiting or 1)  # drain

        # Enter bus protocol mode
        self._send_and_read(b'm\r', delay=0.5)
        mode_num = _TERMINAL_MODE_NUM[protocol]
        resp = self._send_and_read(mode_num.encode() + b'\r', delay=1.0)

        # Accept default settings (send 'y' for "Use previous settings?" prompt)
        if "previous" in resp.lower() or "y/n" in resp.lower():
            resp = self._send_and_read(b'y\r', delay=1.0)

        # Verify we're at the right prompt
        expected_prompt = _TERMINAL_PROMPT[protocol]
        # Read until we see the prompt
        for _ in range(5):
            if expected_prompt in resp:
                break
            resp += self._send_and_read(b'\r', delay=0.5)

        # Open FALA port
        self._fala = serial.Serial(self._fala_port, 115200, timeout=2)
        self._fala.reset_input_buffer()

        self._protocol = protocol
        self._active = True

        # Extract sample rate from activation message if available
        sample_rate = 0
        if "speed:" in resp.lower():
            try:
                for part in resp.split():
                    if part.endswith("Hz"):
                        sample_rate = int(part.replace("Hz", "").replace(",", ""))
                        break
            except (ValueError, IndexError):
                pass

        logger.info("FALA activated: protocol=%s sample_rate=%d", protocol, sample_rate)
        return {
            "protocol": protocol,
            "terminal_port": self._terminal_port,
            "fala_port": self._fala_port,
            "sample_rate_hz": sample_rate,
        }

    def execute(self, command: str) -> dict:
        """Run a bus command on terminal, read FALA capture from binary port."""
        if not self._active:
            raise RuntimeError("FALA session not active")

        # Clear FALA buffer
        if self._fala.in_waiting:
            self._fala.read(self._fala.in_waiting)

        # Send command to terminal
        resp = self._send_and_read(command.encode() + b'\r', delay=1.0)

        # Strip echo and prompt from response
        terminal_output = self._clean_terminal_output(resp, command)

        # Read FALA notification
        notification = self._read_fala_notification(timeout=5.0)
        self._last_notification = notification

        # Dump raw samples if any were captured
        raw_samples = b''
        if notification and notification.get("samples", 0) > 0:
            raw_samples = self._dump_samples(notification["samples"])

        return {
            "terminal_output": terminal_output,
            "capture": {
                "notification": notification,
                "raw_bytes": len(raw_samples),
                "raw": raw_samples,
            },
        }

    def deactivate(self) -> None:
        """Switch back to BPIO2 binmode."""
        if not self._active:
            return

        try:
            # Exit bus mode -> HiZ
            self._send_and_read(b'm\r', delay=0.5)
            self._send_and_read(b'1\r', delay=0.5)

            # Switch binmode back to BPIO2 (2)
            self._send_and_read(b'binmode\r', delay=1.0)
            resp = self._send_and_read(b'2\r', delay=1.0)
            if "Save" in resp:
                self._send_and_read(b'n\r', delay=0.5)

            logger.info("FALA deactivated, BPIO2 restored")
        except Exception as exc:
            logger.warning("Error during FALA deactivation: %s", exc)
        finally:
            self._close_ports()
            self._active = False
            self._protocol = None

    def _dismiss_vt100(self) -> str:
        """Handle VT100 prompt. Returns prompt text once past it."""
        # Send Ctrl-C first to clear any stuck state
        self._term.write(b'\x03')
        time.sleep(0.2)

        for attempt in range(10):
            self._term.write(b'n\r')
            time.sleep(0.3)
            data = self._term.read(self._term.in_waiting or 1)
            resp = data.decode("utf-8", errors="replace")
            if "HiZ>" in resp:
                return resp
            # Sometimes we need a bare CR to get the prompt
            self._term.write(b'\r')
            time.sleep(0.3)
            data = self._term.read(self._term.in_waiting or 1)
            resp = data.decode("utf-8", errors="replace")
            if "HiZ>" in resp:
                return resp

        raise TimeoutError("Could not dismiss VT100 prompt after 10 attempts")

    def _send_and_read(self, data: bytes, delay: float = 0.5) -> str:
        """Send bytes to terminal, wait, read response."""
        self._term.write(data)
        time.sleep(delay)
        response = self._term.read(self._term.in_waiting or 1)
        return response.decode("utf-8", errors="replace")

    def _read_fala_notification(self, timeout: float = 5.0) -> dict | None:
        """Read and parse $FALADATA notification from ACM1."""
        deadline = time.monotonic() + timeout
        buf = b''
        while time.monotonic() < deadline:
            if self._fala.in_waiting:
                buf += self._fala.read(self._fala.in_waiting)
                text = buf.decode("utf-8", errors="replace")
                if "$FALADATA" in text:
                    # Find the complete notification line
                    for line in text.split("\n"):
                        if line.startswith("$FALADATA"):
                            return parse_fala_notification(line.strip())
            time.sleep(0.05)
        logger.warning("No FALA notification received within %.1fs", timeout)
        return None

    def _dump_samples(self, expected_count: int = 0) -> bytes:
        """Send '+' to ACM1, read raw sample bytes."""
        self._fala.write(b'+')
        time.sleep(0.1)

        data = b''
        deadline = time.monotonic() + 3.0  # 3s timeout
        while time.monotonic() < deadline:
            if self._fala.in_waiting:
                data += self._fala.read(self._fala.in_waiting)
                if expected_count > 0 and len(data) >= expected_count:
                    break
            time.sleep(0.05)

        return data[:expected_count] if expected_count > 0 else data

    def _clean_terminal_output(self, resp: str, command: str) -> str:
        """Strip echo, ANSI codes, and prompts from terminal response."""
        # Strip ANSI escape sequences
        clean = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', resp)
        clean = re.sub(r'\x1b[78]', '', clean)
        clean = re.sub(r'\x1b\([AB]', '', clean)
        clean = re.sub(r'\x03', '', clean)

        lines = []
        for line in clean.split('\n'):
            stripped = line.strip()
            if not stripped:
                continue
            # Skip echoed command
            if stripped == command.strip():
                continue
            # Skip prompt lines
            skip = False
            for prompt in _TERMINAL_PROMPT.values():
                if stripped.endswith(prompt) and len(stripped) <= len(prompt) + 5:
                    skip = True
                    break
                # Also skip lines that are just the prompt with the command echoed
                if prompt in stripped and command.strip() in stripped:
                    skip = True
                    break
            if not skip:
                lines.append(stripped)
        return '\n'.join(lines)

    def _close_ports(self) -> None:
        """Close serial ports safely."""
        for port in (self._term, self._fala):
            if port is not None:
                try:
                    port.close()
                except Exception:
                    pass
        self._term = None
        self._fala = None

