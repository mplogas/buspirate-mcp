"""Thin abstraction over the BPIO2 SDK.

Only this module imports from the BPIO2 SDK (bpio_client, bpio_uart).
Everything else in the codebase talks to this module. This makes
testing without hardware trivial -- mock this, not the SDK.
"""

from __future__ import annotations

import sys
from glob import glob
from pathlib import Path
from typing import Any

# Add vendored BPIO2 SDK to sys.path so we can import it.
# The SDK lives at vendor/bpio2/python/pybpio/ relative to the package root.
_VENDOR_PATH = str(
    Path(__file__).resolve().parents[2] / "vendor" / "bpio2" / "python"
)
if _VENDOR_PATH not in sys.path:
    sys.path.insert(0, _VENDOR_PATH)

try:
    from pybpio.bpio_client import BPIOClient
    from pybpio.bpio_uart import BPIOUART
except ImportError:
    # SDK not available -- tests use mocks, but fail fast at runtime
    BPIOClient = None  # type: ignore[assignment, misc]
    BPIOUART = None  # type: ignore[assignment, misc]


class BusPirateHardware:
    """Wraps a single BusPirate 6 connection."""

    def __init__(self, client: Any, uart: Any) -> None:
        self.client = client
        self.uart = uart
        self._last_voltage_mv: int = 0
        self._last_current_ma: int = 0

    @staticmethod
    def list_devices() -> list[dict[str, str]]:
        """Find BusPirate devices on USB CDC serial ports.

        BP6 exposes two USB CDC ports: first is the text terminal,
        second is the binary protocol (BPIO2). We identify by order.
        """
        paths = sorted(glob("/dev/ttyACM*"))
        devices = []
        for i, p in enumerate(paths):
            if len(paths) >= 2:
                role = "terminal" if i % 2 == 0 else "binary"
            else:
                role = "unknown"
            devices.append({"path": p, "role": role})
        return devices

    @classmethod
    def connect(cls, port: str) -> BusPirateHardware:
        """Open a connection to a BusPirate 6 on the given serial port."""
        if BPIOClient is None:
            raise ImportError(
                "BPIO2 SDK not installed. "
                "See https://docs.buspirate.com/docs/binmode-reference/protocol-bpio2/"
            )
        try:
            client = BPIOClient(port)
            uart = BPIOUART(client)
            return cls(client=client, uart=uart)
        except Exception as exc:
            raise ConnectionError(str(exc)) from exc

    def configure_uart(
        self,
        speed: int = 115200,
        data_bits: int = 8,
        parity: bool = False,
        stop_bits: int = 1,
    ) -> None:
        """Configure UART mode on the BusPirate."""
        self.uart.configure(
            speed=speed,
            data_bits=data_bits,
            parity=parity,
            stop_bits=stop_bits,
            flow_control=False,
            signal_inversion=False,
            async_callback=None,
        )

    def read(self) -> bytes:
        """Read whatever is in the UART receive buffer."""
        return self.uart.read_async()

    def write(self, data: bytes) -> None:
        """Send data over UART to the target."""
        self.uart.transfer(data, read_bytes=0)

    def set_voltage(self, voltage_v: float, current_limit_ma: int) -> bool:
        """Set the power supply voltage and current limit."""
        voltage_mv = round(voltage_v * 1000)
        self._last_voltage_mv = voltage_mv
        self._last_current_ma = current_limit_ma
        return self.uart.set_psu_enable(
            voltage_mv=voltage_mv, current_ma=current_limit_ma,
        )

    def set_power(self, enable: bool) -> bool:
        """Enable or disable the power supply.

        When enabling, re-applies the last configured voltage/current.
        Raises RuntimeError if no voltage was previously configured.
        """
        if enable:
            if self._last_voltage_mv == 0:
                raise RuntimeError(
                    "No voltage configured. Call set_voltage() before set_power(True)."
                )
            return self.uart.set_psu_enable(
                voltage_mv=self._last_voltage_mv,
                current_ma=self._last_current_ma,
            )
        return self.uart.set_psu_disable()

    def configure_pin_input(self, pin: int) -> None:
        """Configure a pin as digital input for reading."""
        mask = 1 << pin
        self.uart.set_io_direction(direction_mask=mask, direction=0)

    def get_pin_voltages(self) -> list[int]:
        """Read ADC millivolt values for all IO pins."""
        return self.uart.get_adc_mv()

    def set_pin_output(self, pin: int, high: bool) -> None:
        """Set a pin as output and drive it high or low."""
        mask = 1 << pin
        self.uart.set_io_direction(direction_mask=mask, direction=mask)
        self.uart.set_io_value(value_mask=mask, value=mask if high else 0)

    def release_pin(self, pin: int) -> None:
        """Release a pin back to input (high-impedance)."""
        mask = 1 << pin
        self.uart.set_io_direction(direction_mask=mask, direction=0)

    @staticmethod
    def find_terminal_port() -> str | None:
        """Find the BP6 terminal port (first ACM device)."""
        devices = BusPirateHardware.list_devices()
        for d in devices:
            if d["role"] == "terminal":
                return d["path"]
        return devices[0]["path"] if devices else None

    def disconnect(self) -> None:
        """Disable PSU and clean up the connection."""
        if self.uart is not None:
            try:
                self.uart.set_psu_disable()
            except Exception:
                pass  # best effort -- hardware may already be gone
        self.client = None
        self.uart = None
