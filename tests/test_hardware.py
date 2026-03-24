"""Tests for the BPIO2 hardware abstraction layer."""

import pytest
from unittest.mock import patch, MagicMock
from buspirate_mcp.hardware import BusPirateHardware


class TestListDevices:
    def test_finds_acm_devices(self, tmp_path):
        with patch("buspirate_mcp.hardware.glob") as mock_glob:
            mock_glob.return_value = ["/dev/ttyACM0", "/dev/ttyACM1"]
            devices = BusPirateHardware.list_devices()
            assert len(devices) == 2
            assert devices[0]["path"] == "/dev/ttyACM0"
            assert devices[1]["path"] == "/dev/ttyACM1"

    def test_identifies_terminal_and_binary_ports(self):
        with patch("buspirate_mcp.hardware.glob") as mock_glob:
            mock_glob.return_value = ["/dev/ttyACM0", "/dev/ttyACM1"]
            devices = BusPirateHardware.list_devices()
            assert devices[0]["role"] == "terminal"
            assert devices[1]["role"] == "binary"

    def test_single_device_marked_unknown(self):
        with patch("buspirate_mcp.hardware.glob") as mock_glob:
            mock_glob.return_value = ["/dev/ttyACM0"]
            devices = BusPirateHardware.list_devices()
            assert devices[0]["role"] == "unknown"

    def test_returns_empty_when_no_devices(self):
        with patch("buspirate_mcp.hardware.glob") as mock_glob:
            mock_glob.return_value = []
            devices = BusPirateHardware.list_devices()
            assert devices == []


class TestConnect:
    def test_connect_creates_client_and_uart(self):
        with patch("buspirate_mcp.hardware.BPIOClient") as mock_client_cls, \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_client = MagicMock()
            mock_client.status_request.return_value = {
                "mode_current": "HiZ",
                "version_hardware_major": 6,
            }
            mock_client_cls.return_value = mock_client
            mock_uart_cls.return_value = MagicMock()

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            assert hw.client is not None
            assert hw.uart is not None
            mock_client_cls.assert_called_once_with("/dev/ttyACM1")

    def test_connect_raises_on_failure(self):
        with patch("buspirate_mcp.hardware.BPIOClient") as mock_client_cls:
            mock_client_cls.side_effect = Exception("USB not found")
            with pytest.raises(ConnectionError, match="USB not found"):
                BusPirateHardware.connect("/dev/ttyACM99")


class TestUARTOperations:
    def test_configure_uart(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            hw.configure_uart(speed=9600)
            mock_uart.configure.assert_called_once_with(
                speed=9600, data_bits=8, parity=False,
                stop_bits=1, flow_control=False,
                signal_inversion=False, async_callback=None,
            )

    def test_read_returns_bytes(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart.read_async.return_value = b"Hello\n"
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            data = hw.read()
            assert data == b"Hello\n"

    def test_write_sends_bytes(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            hw.write(b"AT\r\n")
            mock_uart.transfer.assert_called_once_with(b"AT\r\n", read_bytes=0)


class TestPowerSupply:
    def test_set_voltage(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart.set_psu_enable.return_value = True
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            result = hw.set_voltage(3.3, 300)
            mock_uart.set_psu_enable.assert_called_once_with(
                voltage_mv=3300, current_ma=300,
            )
            assert result is True

    def test_set_voltage_uses_round(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart.set_psu_enable.return_value = True
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            hw.set_voltage(2.3, 100)
            # round(2.3 * 1000) = 2300, not int(2.3 * 1000) = 2299
            mock_uart.set_psu_enable.assert_called_once_with(
                voltage_mv=2300, current_ma=100,
            )

    def test_set_power_on_uses_last_voltage(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart.set_psu_enable.return_value = True
            mock_uart.set_psu_disable.return_value = True
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            hw.set_voltage(1.8, 100)
            hw.set_power(False)
            hw.set_power(True)
            # Should re-apply 1.8V, not SDK default 3.3V
            calls = mock_uart.set_psu_enable.call_args_list
            assert calls[-1].kwargs == {"voltage_mv": 1800, "current_ma": 100}

    def test_set_power_on_without_voltage_raises(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            with pytest.raises(RuntimeError, match="No voltage configured"):
                hw.set_power(True)

    def test_get_pin_voltages(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart.get_adc_mv.return_value = [0, 0, 0, 0, 3300, 3280, 0, 0]
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            voltages = hw.get_pin_voltages()
            assert voltages == [0, 0, 0, 0, 3300, 3280, 0, 0]


class TestDisconnect:
    def test_disconnect_disables_psu(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            hw.disconnect()
            mock_uart.set_psu_disable.assert_called_once()
            assert hw.client is None
            assert hw.uart is None

    def test_disconnect_safe_when_uart_gone(self):
        with patch("buspirate_mcp.hardware.BPIOClient"), \
             patch("buspirate_mcp.hardware.BPIOUART") as mock_uart_cls:
            mock_uart = MagicMock()
            mock_uart.set_psu_disable.side_effect = Exception("USB gone")
            mock_uart_cls.return_value = mock_uart

            hw = BusPirateHardware.connect("/dev/ttyACM1")
            hw.disconnect()  # should not raise
            assert hw.uart is None
