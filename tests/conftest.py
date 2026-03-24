"""Shared test fixtures for buspirate-mcp tests."""

import pytest


class FakeUART:
    """Mock BPIO2 UART interface for testing without hardware."""

    def __init__(self):
        self.configured = False
        self.config = {}
        self._rx_buffer = b""
        self._tx_log = []

    def configure(self, speed=115200, data_bits=8, parity=False,
                  stop_bits=1, flow_control=False,
                  signal_inversion=False, async_callback=None):
        self.configured = True
        self.config = {
            "speed": speed,
            "data_bits": data_bits,
            "parity": parity,
            "stop_bits": stop_bits,
        }

    def transfer(self, data, read_bytes=0):
        self._tx_log.append(data)
        result = self._rx_buffer[:read_bytes]
        self._rx_buffer = self._rx_buffer[read_bytes:]
        return result

    def read_async(self):
        data = self._rx_buffer
        self._rx_buffer = b""
        return data

    def set_psu_enable(self, voltage_mv=3300, current_ma=300):
        return True

    def set_psu_disable(self):
        return True

    def set_io_direction(self, direction_mask=0, direction=0):
        pass

    def get_adc_mv(self):
        return [0, 0, 0, 0, 3300, 3300, 0, 0]

    def get_psu_measured_mv(self):
        return 3300

    def inject_rx(self, data: bytes):
        """Test helper: simulate data arriving from target device."""
        self._rx_buffer += data


class FakeClient:
    """Mock BPIO2 client for testing without hardware."""

    def __init__(self, port="fake"):
        self.port = port

    def status_request(self):
        return {
            "mode_current": "HiZ",
            "version_hardware_major": 6,
            "version_firmware": "1.0.0",
        }


@pytest.fixture
def fake_client():
    return FakeClient()


@pytest.fixture
def fake_uart():
    return FakeUART()
