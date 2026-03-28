"""Thin abstraction over the BPIO2 SDK.

Only this module imports from the BPIO2 SDK (bpio_client, bpio_uart, etc.).
Everything else in the codebase talks to this module. This makes
testing without hardware trivial -- mock this, not the SDK.
"""

from __future__ import annotations

import io
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
    from pybpio.bpio_spi import BPIOSPI
    from pybpio.bpio_i2c import BPIOI2C
    from pybpio.bpio_1wire import BPIO1Wire
except ImportError:
    # SDK not available -- tests use mocks, but fail fast at runtime
    BPIOClient = None  # type: ignore[assignment, misc]
    BPIOUART = None  # type: ignore[assignment, misc]
    BPIOSPI = None  # type: ignore[assignment, misc]
    BPIOI2C = None  # type: ignore[assignment, misc]
    BPIO1Wire = None  # type: ignore[assignment, misc]


class BusPirateHardware:
    """Wraps a single BusPirate 6 connection."""

    def __init__(self, client: Any, uart: Any) -> None:
        self.client = client
        self.uart = uart
        self.spi: Any = None
        self.i2c: Any = None
        self.onewire: Any = None
        self._active_mode: str | None = None
        self._last_voltage_mv: int = 0
        self._last_current_ma: int = 0

    @property
    def _active_protocol(self) -> Any:
        """Return the protocol object for the currently active mode.

        Falls back to self.uart when no mode is set (backward compat for
        PSU calls before any protocol is configured).
        """
        if self._active_mode == "spi":
            return self.spi
        if self._active_mode == "i2c":
            return self.i2c
        if self._active_mode == "1wire":
            return self.onewire
        # "uart" or None -- use uart as default
        return self.uart

    def _check_mode(self, target_mode: str) -> None:
        """Raise if a different mode is already active.

        Reconfiguring the same mode is allowed. Switching requires
        reset_mode() first.
        """
        if self._active_mode is not None and self._active_mode != target_mode:
            raise RuntimeError(
                f"Mode '{self._active_mode}' is active. "
                f"Call reset_mode() before switching to '{target_mode}'."
            )

    def reset_mode(self) -> None:
        """Clear the active mode so a different protocol can be configured."""
        self._active_mode = None

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
        self._check_mode("uart")
        self.uart.configure(
            speed=speed,
            data_bits=data_bits,
            parity=parity,
            stop_bits=stop_bits,
            flow_control=False,
            signal_inversion=False,
            async_callback=None,
        )
        self._active_mode = "uart"

    def read(self) -> bytes:
        """Read whatever is in the UART receive buffer."""
        return self.uart.read_async()

    def write(self, data: bytes) -> None:
        """Send data over UART to the target."""
        self.uart.transfer(data, read_bytes=0)

    def set_voltage(self, voltage_v: float, current_limit_ma: int) -> bool:
        """Set the power supply voltage and current limit."""
        proto = self._active_protocol
        voltage_mv = round(voltage_v * 1000)
        self._last_voltage_mv = voltage_mv
        self._last_current_ma = current_limit_ma
        return proto.set_psu_enable(
            voltage_mv=voltage_mv, current_ma=current_limit_ma,
        )

    def set_power(self, enable: bool) -> bool:
        """Enable or disable the power supply.

        When enabling, re-applies the last configured voltage/current.
        Raises RuntimeError if no voltage was previously configured.
        """
        proto = self._active_protocol
        if enable:
            if self._last_voltage_mv == 0:
                raise RuntimeError(
                    "No voltage configured. Call set_voltage() before set_power(True)."
                )
            return proto.set_psu_enable(
                voltage_mv=self._last_voltage_mv,
                current_ma=self._last_current_ma,
            )
        return proto.set_psu_disable()

    def configure_pin_input(self, pin: int) -> None:
        """Configure a pin as digital input for reading."""
        proto = self._active_protocol
        mask = 1 << pin
        proto.set_io_direction(direction_mask=mask, direction=0)

    def get_pin_voltages(self) -> list[int]:
        """Read ADC millivolt values for all IO pins."""
        return self._active_protocol.get_adc_mv()

    def set_pin_output(self, pin: int, high: bool) -> None:
        """Set a pin as output and drive it high or low."""
        proto = self._active_protocol
        mask = 1 << pin
        proto.set_io_direction(direction_mask=mask, direction=mask)
        proto.set_io_value(value_mask=mask, value=mask if high else 0)

    def release_pin(self, pin: int) -> None:
        """Release a pin back to input (high-impedance)."""
        proto = self._active_protocol
        mask = 1 << pin
        proto.set_io_direction(direction_mask=mask, direction=0)

    # -- SPI --

    def configure_spi(
        self,
        speed: int = 1000000,
        clock_polarity: bool = False,
        clock_phase: bool = False,
        chip_select_idle: bool = True,
        voltage_mv: int | None = None,
        current_ma: int | None = None,
    ) -> None:
        """Configure SPI mode on the BusPirate."""
        self._check_mode("spi")
        if self.spi is None:
            self.spi = BPIOSPI(self.client)
        kwargs: dict[str, Any] = {}
        if voltage_mv is not None:
            kwargs["psu_enable"] = True
            kwargs["psu_set_mv"] = voltage_mv
            kwargs["psu_set_ma"] = current_ma or 100
        self.spi.configure(
            speed=speed,
            clock_polarity=clock_polarity,
            clock_phase=clock_phase,
            chip_select_idle=chip_select_idle,
            **kwargs,
        )
        self._active_mode = "spi"

    def spi_transfer(self, write_data: bytes, read_bytes: int = 0) -> bytes:
        """Send/receive data over SPI."""
        return self.spi.transfer(write_data=write_data, read_bytes=read_bytes)

    def spi_select(self) -> None:
        """Assert chip select (active low)."""
        return self.spi.select()

    def spi_deselect(self) -> None:
        """Deassert chip select."""
        return self.spi.deselect()

    # -- I2C --

    def configure_i2c(
        self,
        speed: int = 400000,
        clock_stretch: bool = False,
        voltage_mv: int | None = None,
        current_ma: int | None = None,
    ) -> None:
        """Configure I2C mode on the BusPirate."""
        self._check_mode("i2c")
        if self.i2c is None:
            self.i2c = BPIOI2C(self.client)
        kwargs: dict[str, Any] = {"pullup_enable": True}
        if voltage_mv is not None:
            kwargs["psu_enable"] = True
            kwargs["psu_set_mv"] = voltage_mv
            kwargs["psu_set_ma"] = current_ma or 100
        self.i2c.configure(speed=speed, clock_stretch=clock_stretch, **kwargs)
        self._active_mode = "i2c"

    def i2c_transfer(self, write_data: bytes, read_bytes: int = 0) -> bytes:
        """Send/receive data over I2C."""
        return self.i2c.transfer(write_data=write_data, read_bytes=read_bytes)

    def i2c_scan(self, start_addr: int = 0x00, end_addr: int = 0x7F) -> list:
        """Scan the I2C bus for devices.

        The SDK scan() has print() calls that would corrupt MCP stdio
        transport, so we redirect stdout during the call.
        """
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            return self.i2c.scan(start_addr=start_addr, end_addr=end_addr)
        finally:
            sys.stdout = old_stdout

    # -- 1-Wire --

    def configure_1wire(
        self,
        voltage_mv: int | None = None,
        current_ma: int | None = None,
    ) -> None:
        """Configure 1-Wire mode on the BusPirate."""
        self._check_mode("1wire")
        if self.onewire is None:
            self.onewire = BPIO1Wire(self.client)
        kwargs: dict[str, Any] = {"pullup_enable": True}
        if voltage_mv is not None:
            kwargs["psu_enable"] = True
            kwargs["psu_set_mv"] = voltage_mv
            kwargs["psu_set_ma"] = current_ma or 100
        self.onewire.configure(**kwargs)
        self._active_mode = "1wire"

    def onewire_reset(self) -> Any:
        """Send a 1-Wire bus reset pulse."""
        return self.onewire.reset()

    def onewire_transfer(self, write_data: bytes, read_bytes: int = 0) -> bytes:
        """Send/receive data over 1-Wire."""
        return self.onewire.transfer(write_data=write_data, read_bytes=read_bytes)

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
        proto = self._active_protocol
        if proto is not None:
            try:
                proto.set_psu_disable()
            except Exception:
                pass  # best effort -- hardware may already be gone
        self.client = None
        self.uart = None
        self.spi = None
        self.i2c = None
        self.onewire = None
        self._active_mode = None
