"""Tests for MCP tool implementations."""

import pytest
from unittest.mock import patch, MagicMock
from buspirate_mcp.tools import (
    tool_list_devices,
    tool_verify_connection,
    tool_scan_baud,
    tool_open_uart,
    tool_read_output,
    tool_send_command,
    tool_close_uart,
    tool_set_voltage,
    tool_set_power,
    tool_enter_download_mode,
    tool_read_flash,
    _ascii_score,
)
from buspirate_mcp.session import SessionManager


class TestAsciiScore:
    def test_empty_data(self):
        assert _ascii_score(b"") == 0.0

    def test_all_printable(self):
        assert _ascii_score(b"Hello World\n") == 1.0

    def test_all_garbage(self):
        assert _ascii_score(bytes(range(128, 160))) < 0.1

    def test_mixed(self):
        data = b"Hello" + bytes(range(128, 133))
        score = _ascii_score(data)
        assert 0.4 < score < 0.6

    def test_tabs_and_newlines_are_printable(self):
        assert _ascii_score(b"\t\n\r") == 1.0


class TestListDevices:
    @pytest.mark.asyncio
    async def test_returns_device_list(self):
        with patch("buspirate_mcp.tools.BusPirateHardware") as mock_hw:
            mock_hw.list_devices.return_value = [
                {"path": "/dev/ttyACM0"},
                {"path": "/dev/ttyACM1"},
            ]
            result = await tool_list_devices()
            assert len(result["devices"]) == 2

    @pytest.mark.asyncio
    async def test_returns_empty_with_hint(self):
        with patch("buspirate_mcp.tools.BusPirateHardware") as mock_hw:
            mock_hw.list_devices.return_value = []
            result = await tool_list_devices()
            assert result["devices"] == []
            assert "hint" in result


class TestVerifyConnection:
    @pytest.mark.asyncio
    async def test_detects_activity(self):
        mock_hw = MagicMock()
        mock_hw.get_pin_voltages.side_effect = [
            [0, 0, 0, 0, 3300, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0],
            [0, 0, 0, 0, 3300, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 0],
        ]
        result = await tool_verify_connection(
            hardware=mock_hw, pins={"tx": 4, "rx": 5}, sample_duration_ms=100,
        )
        assert result["activity_detected"] is True

    @pytest.mark.asyncio
    async def test_no_activity_on_quiescent_target(self):
        mock_hw = MagicMock()
        mock_hw.get_pin_voltages.return_value = [0, 0, 0, 0, 3300, 0, 0, 0]
        result = await tool_verify_connection(
            hardware=mock_hw, pins={"tx": 4, "rx": 5}, sample_duration_ms=100,
        )
        assert result["activity_detected"] is False

    @pytest.mark.asyncio
    async def test_handles_none_voltages(self):
        mock_hw = MagicMock()
        mock_hw.get_pin_voltages.return_value = None
        result = await tool_verify_connection(
            hardware=mock_hw, pins={"tx": 4, "rx": 5}, sample_duration_ms=100,
        )
        assert result["activity_detected"] is False
        assert result["samples_taken"] == 0

    @pytest.mark.asyncio
    async def test_configures_uart_before_sampling(self):
        mock_hw = MagicMock()
        mock_hw.get_pin_voltages.return_value = [0, 0, 0, 0, 0, 0, 0, 0]
        await tool_verify_connection(
            hardware=mock_hw, pins={"tx": 4, "rx": 5}, sample_duration_ms=100,
        )
        mock_hw.configure_uart.assert_called_once_with(speed=115200)


class TestScanBaud:
    @pytest.mark.asyncio
    async def test_finds_correct_baud(self):
        mock_hw = MagicMock()
        last_speed = [0]

        def track_configure(**kwargs):
            last_speed[0] = kwargs.get("speed", 0)

        def fake_read():
            if last_speed[0] == 9600:
                return b"Linux version 5.15.0\nroot@device:~# "
            return bytes(range(128, 160))

        mock_hw.configure_uart = MagicMock(side_effect=track_configure)
        mock_hw.read = MagicMock(side_effect=fake_read)

        result = await tool_scan_baud(hardware=mock_hw)
        assert result["recommended"] == 9600
        assert result["best_score"] >= 0.7
        assert result["candidates"][0]["baud"] == 9600

    @pytest.mark.asyncio
    async def test_reports_failure_when_no_readable_rate(self):
        mock_hw = MagicMock()
        mock_hw.configure_uart = MagicMock()
        mock_hw.read = MagicMock(return_value=bytes(range(128, 160)))

        result = await tool_scan_baud(hardware=mock_hw)
        assert result["best_score"] < 0.7
        assert result["recommended"] is None

    @pytest.mark.asyncio
    async def test_handles_none_reads(self):
        mock_hw = MagicMock()
        mock_hw.configure_uart = MagicMock()
        mock_hw.read = MagicMock(return_value=None)

        result = await tool_scan_baud(hardware=mock_hw)
        assert result["best_score"] == 0.0
        for c in result["candidates"]:
            assert c["sample_bytes"] == 0


class TestOpenUart:
    @pytest.mark.asyncio
    async def test_creates_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_uart(
            session_manager=mgr,
            hardware=mock_hw,
            baud=115200,
            pins={"tx": 4, "rx": 5},
            engagement_name="test-device",
        )
        assert "session_id" in result
        assert "engagement_path" in result

    @pytest.mark.asyncio
    async def test_passes_device_path(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        result = await tool_open_uart(
            session_manager=mgr,
            hardware=mock_hw,
            baud=115200,
            pins={"tx": 4, "rx": 5},
            engagement_name="test-device",
            device_path="/dev/ttyACM1",
        )
        import json
        config = json.loads(
            (tmp_path / result["engagement_path"].split("/")[-1] / "config.json").read_text()
        )
        assert config["device_path"] == "/dev/ttyACM1"


class TestReadOutput:
    @pytest.mark.asyncio
    async def test_reads_buffered_data(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.read.return_value = b"root@device:~# "
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        result = await tool_read_output(
            session_manager=mgr, session_id=session.session_id, timeout_ms=100,
        )
        assert "root@device" in result["text"]

    @pytest.mark.asyncio
    async def test_returns_empty_on_timeout(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.read.return_value = b""
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        result = await tool_read_output(
            session_manager=mgr, session_id=session.session_id, timeout_ms=100,
        )
        assert result["text"] == ""
        assert result["bytes_received"] == 0

    @pytest.mark.asyncio
    async def test_handles_none_reads(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.read.return_value = None
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        result = await tool_read_output(
            session_manager=mgr, session_id=session.session_id, timeout_ms=100,
        )
        assert result["bytes_received"] == 0


class TestSendCommand:
    @pytest.mark.asyncio
    async def test_sends_and_captures_response(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        mock_hw.read.side_effect = [
            b"Linux device 5.15.0\n",
            b"# ",
        ]
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        result = await tool_send_command(
            session_manager=mgr,
            session_id=session.session_id,
            command="uname -a",
            timeout_ms=500,
        )
        assert "Linux" in result["response"]
        mock_hw.write.assert_called_once()


class TestCloseUart:
    @pytest.mark.asyncio
    async def test_closes_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        mock_hw = MagicMock()
        session = mgr.create(
            name="test", hardware=mock_hw, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        sid = session.session_id
        result = await tool_close_uart(session_manager=mgr, session_id=sid)
        assert result["closed"] is True
        with pytest.raises(KeyError):
            mgr.get(sid)


class TestSetVoltage:
    @pytest.mark.asyncio
    async def test_valid_voltage(self):
        mock_hw = MagicMock()
        mock_hw.set_voltage.return_value = True
        result = await tool_set_voltage(
            hardware=mock_hw, voltage_v=3.3, current_limit_ma=300,
        )
        assert result["applied"] is True
        mock_hw.set_voltage.assert_called_once_with(3.3, 300)

    @pytest.mark.asyncio
    async def test_out_of_range_voltage(self):
        mock_hw = MagicMock()
        result = await tool_set_voltage(
            hardware=mock_hw, voltage_v=12.0, current_limit_ma=300,
        )
        assert result["error"] is not None
        mock_hw.set_voltage.assert_not_called()


class TestSetPower:
    @pytest.mark.asyncio
    async def test_enable_power(self):
        mock_hw = MagicMock()
        mock_hw.set_power.return_value = True
        result = await tool_set_power(hardware=mock_hw, enable=True)
        assert result["applied"] is True

    @pytest.mark.asyncio
    async def test_disable_power(self):
        mock_hw = MagicMock()
        mock_hw.set_power.return_value = True
        result = await tool_set_power(hardware=mock_hw, enable=False)
        assert result["applied"] is True
        assert result["power"] == "off"


class TestEnterDownloadMode:
    @pytest.mark.asyncio
    async def test_toggles_pins(self):
        mock_hw = MagicMock()
        result = await tool_enter_download_mode(
            hardware=mock_hw, boot_pin=0, reset_pin=1,
        )
        assert result["status"] == "download_mode_entered"
        # Verify pin toggle sequence: boot LOW, reset LOW, reset HIGH, release both
        mock_hw.set_pin_output.assert_any_call(0, high=False)
        mock_hw.set_pin_output.assert_any_call(1, high=False)
        mock_hw.set_pin_output.assert_any_call(1, high=True)
        mock_hw.release_pin.assert_any_call(0)
        mock_hw.release_pin.assert_any_call(1)


class TestReadFlash:
    @pytest.mark.asyncio
    async def test_returns_error_when_esptool_missing(self, tmp_path):
        mock_hw = MagicMock()
        output = tmp_path / "dump.bin"
        with patch("buspirate_mcp.tools._enter_bridge_mode", return_value=True), \
             patch("buspirate_mcp.tools.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("esptool not found")
            result = await tool_read_flash(
                hardware=mock_hw,
                terminal_port="/dev/ttyACM0",
                offset=0,
                size=4096,
                output_path=str(output),
            )
            assert "error" in result

    @pytest.mark.asyncio
    async def test_proceeds_even_if_bridge_entry_returns_false(self, tmp_path):
        """Bridge may already be active from a previous call."""
        mock_hw = MagicMock()
        output = tmp_path / "dump.bin"
        output.write_bytes(b'\xff' * 4096)
        with patch("buspirate_mcp.tools._enter_bridge_mode", return_value=False), \
             patch("buspirate_mcp.tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Read 4096 bytes", stderr="",
            )
            result = await tool_read_flash(
                hardware=mock_hw,
                terminal_port="/dev/ttyACM0",
                offset=0,
                size=4096,
                output_path=str(output),
            )
            assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_success_returns_output_path(self, tmp_path):
        mock_hw = MagicMock()
        output = tmp_path / "dump.bin"
        output.write_bytes(b'\xff' * 4096)
        with patch("buspirate_mcp.tools._enter_bridge_mode", return_value=True), \
             patch("buspirate_mcp.tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="Read 4096 bytes", stderr="",
            )
            result = await tool_read_flash(
                hardware=mock_hw,
                terminal_port="/dev/ttyACM0",
                offset=0,
                size=4096,
                output_path=str(output),
            )
            assert result["status"] == "success"
            assert result["bytes_read"] == 4096
