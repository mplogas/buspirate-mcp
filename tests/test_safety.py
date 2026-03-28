"""Tests for the three-tier safety model."""

import pytest
from buspirate_mcp.safety import SafetyTier, classify_tool, validate_voltage_range


class TestClassifyTool:
    def test_read_only_tools(self):
        for tool in [
            "list_devices", "verify_connection", "scan_baud", "read_output",
            "spi_probe", "spi_read", "spi_dump", "spi_transfer",
            "i2c_scan", "i2c_read", "i2c_dump",
            "onewire_search", "onewire_read",
            "la_command", "la_analyze", "la_identify",
        ]:
            assert classify_tool(tool) == SafetyTier.READ_ONLY

    def test_allowed_write_tools(self):
        for tool in [
            "open_uart", "send_command", "close_uart",
            "enter_download_mode", "read_flash",
            "open_spi", "close_spi",
            "open_i2c", "close_i2c",
            "open_1wire", "close_1wire",
            "la_prepare", "la_cleanup",
        ]:
            assert classify_tool(tool) == SafetyTier.ALLOWED_WRITE

    def test_approval_write_tools(self):
        for tool in ["set_voltage", "set_power", "spi_write", "i2c_write"]:
            assert classify_tool(tool) == SafetyTier.APPROVAL_WRITE

    def test_unknown_tool_raises(self):
        with pytest.raises(ValueError, match="Unknown tool"):
            classify_tool("hack_the_planet")


class TestValidateVoltageRange:
    def test_valid_voltage(self):
        assert validate_voltage_range(3.3, 300) is None

    def test_voltage_too_low(self):
        with pytest.raises(ValueError, match="0.8V"):
            validate_voltage_range(0.5, 300)

    def test_voltage_too_high(self):
        with pytest.raises(ValueError, match="5.0V"):
            validate_voltage_range(5.5, 300)

    def test_current_too_high(self):
        with pytest.raises(ValueError, match="500mA"):
            validate_voltage_range(3.3, 600)

    def test_negative_current(self):
        with pytest.raises(ValueError, match="0"):
            validate_voltage_range(3.3, -10)

    def test_boundary_values(self):
        assert validate_voltage_range(0.8, 0) is None
        assert validate_voltage_range(5.0, 500) is None
