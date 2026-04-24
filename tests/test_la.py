"""Tests for FALA (Follow Along Logic Analyzer) session management."""

import pytest
from unittest.mock import patch, MagicMock
from buspirate_mcp.la import FALASession
from buspirate_mcp.la_parsers import parse_fala_notification


class FakeTerminal:
    """Mock serial port for BP6 terminal (ACM0).

    State machine that responds to binmode and mode navigation.
    """

    def __init__(self):
        self.in_waiting = 0
        self._responses = []
        self._write_log = []
        self._state = "vt100"  # vt100 -> hiz -> binmode -> fala -> mode_menu -> protocol

    def reset_input_buffer(self):
        pass

    def reset_output_buffer(self):
        pass

    def write(self, data):
        self._write_log.append(data)
        text = data.decode("utf-8", errors="replace").strip()

        if self._state == "vt100":
            if text in ('n', '\x03'):
                self._responses.append(b'n\r\nHiZ> ')
                self._state = "hiz"
            else:
                self._responses.append(b'\r\nVT100 compatible color mode? (Y/n)> ')

        elif self._state == "hiz":
            if text == 'binmode':
                self._responses.append(
                    b'binmode\r\n\r\n 1. SUMP\r\n 2. BPIO2\r\n 3. SWIO\r\n'
                    b' 4. Follow along logic analyzer\r\n'
                    b'Select binary mode\r\nx to exit (1) > '
                )
                self._state = "binmode"
            elif text == 'm':
                self._responses.append(
                    b'm\r\nMode selection\r\n 1. HiZ\r\n 6. SPI\r\n'
                    b'x to exit > '
                )
                self._state = "mode_menu"
            elif text == '':
                self._responses.append(b'\r\nHiZ> ')
            else:
                self._responses.append(b'\r\nHiZ> ')

        elif self._state == "binmode":
            if text == '4':
                self._responses.append(
                    b'4\r\nBinmode selected: Follow along logic analyzer\r\n'
                    b'Save setting?\r\ny/n > '
                )
                self._state = "fala_save"
            elif text == '2':
                self._responses.append(
                    b'2\r\nBinmode selected: BPIO2 flatbuffer interface\r\n'
                    b'Save setting?\r\ny/n > '
                )
                self._state = "bpio2_save"
            else:
                self._responses.append(b'Invalid\r\n')

        elif self._state == "fala_save":
            if text == 'n':
                self._responses.append(
                    b'n\r\nLogic analyzer speed: 75000000Hz (8x oversampling)\r\n'
                    b'HiZ> '
                )
                self._state = "hiz"
            else:
                self._responses.append(b'\r\n')

        elif self._state == "bpio2_save":
            if text == 'n':
                self._responses.append(b'n\r\nHiZ> ')
                self._state = "hiz"

        elif self._state == "mode_menu":
            if text == '6':  # SPI
                self._responses.append(
                    b'6\r\nMode: SPI\r\nUse previous settings?\r\n'
                    b'y/n, x to exit (Y) > '
                )
                self._state = "spi_config"
            elif text == '1':  # HiZ
                self._responses.append(b'1\r\nHiZ> ')
                self._state = "hiz"
            else:
                self._responses.append(b'\r\n')

        elif self._state == "spi_config":
            if text == 'y':
                self._responses.append(
                    b'y\r\nLogic analyzer speed: 75000000Hz\r\nSPI> '
                )
                self._state = "spi"
            else:
                self._responses.append(b'\r\nSPI> ')
                self._state = "spi"

        elif self._state == "spi":
            if text == 'm':
                self._responses.append(
                    b'm\r\nMode selection\r\n 1. HiZ\r\n'
                    b'x to exit > '
                )
                self._state = "mode_menu"
            elif text.startswith('['):
                # Bus command
                self._responses.append(
                    f'{text}\r\nCS Enabled\r\nTX: 0x9F\r\n'
                    f'RX: 0xEF 0x40 0x18\r\nCS Disabled\r\n'
                    f'Logic analyzer: 7468 samples captured\r\nSPI> '.encode()
                )
            else:
                self._responses.append(b'\r\nSPI> ')

        self.in_waiting = sum(len(r) for r in self._responses)

    def read(self, n):
        if not self._responses:
            return b''
        data = b''.join(self._responses)
        self._responses.clear()
        self.in_waiting = 0
        return data[:n] if n < len(data) else data

    def close(self):
        pass


class FakeFALA:
    """Mock serial port for FALA data (ACM1).

    Auto-provides notification data after the first buffer clear (simulating
    the real FALA which sends $FALADATA after a bus command completes).
    """

    def __init__(self):
        self._data = b''
        self._write_log = []
        self._notification = b'$FALADATA;8;0;0;N;75000000;7468;0;\n'
        self._samples = bytes([0b00010100] * 7468)
        self._arm_countdown = 0

    @property
    def in_waiting(self):
        # Deliver notification on the Nth check after arming (skip the first
        # check which is the buffer clear in execute())
        if self._arm_countdown > 0:
            self._arm_countdown -= 1
            if self._arm_countdown == 0 and not self._data:
                self._data = self._notification
        return len(self._data)

    def arm(self):
        """Arm auto-notification. Skips 1 in_waiting check (the clear), delivers on the 2nd."""
        self._arm_countdown = 2

    def reset_input_buffer(self):
        self._data = b''

    def write(self, data):
        self._write_log.append(data)
        if data == b'+':
            self._data = self._samples

    def read(self, n):
        if not self._data:
            return b''
        result = self._data[:n]
        self._data = self._data[n:]
        return result

    def close(self):
        pass


class TestParseNotification:
    def test_valid_notification(self):
        result = parse_fala_notification('$FALADATA;8;0;0;N;75000000;7468;0;')
        assert result["channels"] == 8
        assert result["sample_rate_hz"] == 75000000
        assert result["samples"] == 7468
        assert result["edge_trigger"] is False
        assert result["pre_samples"] == 0

    def test_edge_trigger_enabled(self):
        result = parse_fala_notification('$FALADATA;8;1;1;Y;50000000;1000;500;')
        assert result["edge_trigger"] is True
        assert result["trigger_pin"] == 1
        assert result["trigger_mask"] == 1
        assert result["pre_samples"] == 500

    def test_invalid_notification(self):
        result = parse_fala_notification('GARBAGE')
        assert "error" in result

    def test_truncated_notification(self):
        result = parse_fala_notification('$FALADATA;8;0')
        assert "error" in result


@patch('buspirate_mcp.la.time.sleep')
class TestFALASessionActivate:
    def test_activate_spi(self, _mock_sleep):
        fake_term = FakeTerminal()
        fake_fala = FakeFALA()

        with patch('buspirate_mcp.la.serial') as mock_serial:
            def create_serial(port, *args, **kwargs):
                if 'ACM0' in port:
                    return fake_term
                return fake_fala
            mock_serial.Serial.side_effect = create_serial

            session = FALASession('/dev/ttyACM0', '/dev/ttyACM1')
            result = session.activate('spi')

        assert session.active is True
        assert session.protocol == 'spi'
        assert result['protocol'] == 'spi'

    def test_activate_invalid_protocol(self, _mock_sleep):
        session = FALASession('/dev/ttyACM0', '/dev/ttyACM1')
        with pytest.raises(ValueError, match="Unsupported protocol"):
            session.activate('can_bus')


@patch('buspirate_mcp.la.time.sleep')
class TestFALASessionExecute:
    def test_execute_spi_command(self, _mock_sleep):
        fake_term = FakeTerminal()
        fake_fala = FakeFALA()

        with patch('buspirate_mcp.la.serial') as mock_serial:
            def create_serial(port, *args, **kwargs):
                if 'ACM0' in port:
                    return fake_term
                return fake_fala
            mock_serial.Serial.side_effect = create_serial

            session = FALASession('/dev/ttyACM0', '/dev/ttyACM1')
            session.activate('spi')

            # Arm FALA to deliver notification on next in_waiting check
            fake_fala.arm()

            result = session.execute('[0x9f r:3]')

        assert 'terminal_output' in result
        assert result['capture']['notification'] is not None
        assert result['capture']['notification']['samples'] == 7468
        assert result['capture']['raw_bytes'] == 7468


@patch('buspirate_mcp.la.time.sleep')
class TestFALASessionDeactivate:
    def test_deactivate_restores_bpio2(self, _mock_sleep):
        fake_term = FakeTerminal()
        fake_fala = FakeFALA()

        with patch('buspirate_mcp.la.serial') as mock_serial:
            def create_serial(port, *args, **kwargs):
                if 'ACM0' in port:
                    return fake_term
                return fake_fala
            mock_serial.Serial.side_effect = create_serial

            session = FALASession('/dev/ttyACM0', '/dev/ttyACM1')
            session.activate('spi')
            session.deactivate()

        assert session.active is False
        assert session.protocol is None

    def test_deactivate_when_not_active(self, _mock_sleep):
        session = FALASession('/dev/ttyACM0', '/dev/ttyACM1')
        session.deactivate()  # should not raise
        assert session.active is False
