"""Tests for FALA signal analysis and protocol identification.

All pure function tests with synthetic data. No serial or hardware needed.
"""

from __future__ import annotations

import pytest

from buspirate_mcp.la_parsers import (
    analyze_channels,
    count_transitions,
    extract_channel,
    extract_i2c_frames,
    extract_spi_frames,
    extract_uart_data,
    identify_protocol,
    parse_fala_notification,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _build_samples(channel_states: dict[int, list[int]]) -> bytes:
    """Build raw sample bytes from per-channel state lists.

    channel_states: {channel_num: [bit_values...]}
    All channels must have the same length.
    """
    length = len(next(iter(channel_states.values())))
    samples = []
    for i in range(length):
        byte_val = 0
        for ch, states in channel_states.items():
            byte_val |= (states[i] << ch)
        samples.append(byte_val)
    return bytes(samples)


def _repeat(value: int, count: int) -> list[int]:
    """Shorthand for [value] * count."""
    return [value] * count


# ---------------------------------------------------------------------------
# TestParseNotification
# ---------------------------------------------------------------------------

class TestParseNotification:
    def test_valid(self):
        result = parse_fala_notification("$FALADATA;8;0;0;N;75000000;7468;0;")
        assert result == {
            "channels": 8,
            "trigger_pin": 0,
            "trigger_mask": 0,
            "edge_trigger": False,
            "sample_rate_hz": 75_000_000,
            "samples": 7468,
            "pre_samples": 0,
        }

    def test_with_edge_trigger(self):
        result = parse_fala_notification("$FALADATA;8;2;1;Y;75000000;1024;128;")
        assert result["edge_trigger"] is True
        assert result["trigger_pin"] == 2
        assert result["trigger_mask"] == 1
        assert result["pre_samples"] == 128

    def test_invalid_header(self):
        result = parse_fala_notification("$GARBAGE;8;0;0;N;75000000;7468;0;")
        assert "error" in result

    def test_truncated(self):
        result = parse_fala_notification("$FALADATA;8;0")
        assert "error" in result


# ---------------------------------------------------------------------------
# TestExtractChannel
# ---------------------------------------------------------------------------

class TestExtractChannel:
    def test_single_channel(self):
        # 0b00000001 = 1, 0b00000000 = 0, 0b00000001 = 1
        raw = bytes([0x01, 0x00, 0x01, 0x00])
        assert extract_channel(raw, 0) == [1, 0, 1, 0]

    def test_channel_7(self):
        # bit 7: 0x80 = 1, 0x00 = 0
        raw = bytes([0x80, 0x00, 0x80])
        assert extract_channel(raw, 7) == [1, 0, 1]


# ---------------------------------------------------------------------------
# TestAnalyzeChannels
# ---------------------------------------------------------------------------

class TestAnalyzeChannels:
    def test_clock_signal(self):
        """50% duty cycle square wave should be identified as clock."""
        # 1 MHz clock at 75 MHz sample rate: 75 samples per cycle
        # 37 or 38 samples high, 37 or 38 samples low
        half = 38
        cycles = 100
        clock_bits = []
        for _ in range(cycles):
            clock_bits.extend(_repeat(1, half))
            clock_bits.extend(_repeat(0, half - 1))
        raw = _build_samples({0: clock_bits})
        result = analyze_channels(raw, 75_000_000, channels=[0])
        ch = result["channels"]["0"]
        assert ch["role"] == "clock"
        assert 0.45 <= ch["duty_cycle"] <= 0.55

    def test_cs_signal(self):
        """Mostly high with brief low pulse should be identified as CS."""
        # 900 samples high, 50 samples low (one brief assertion), 900 high
        cs_bits = _repeat(1, 900) + _repeat(0, 50) + _repeat(1, 900)
        raw = _build_samples({0: cs_bits})
        result = analyze_channels(raw, 75_000_000, channels=[0])
        ch = result["channels"]["0"]
        assert ch["role"] == "cs"
        assert ch["idle_state"] == "high"

    def test_inactive_channel(self):
        """All zeros should be inactive."""
        raw = _build_samples({0: _repeat(0, 1000)})
        result = analyze_channels(raw, 75_000_000, channels=[0])
        assert result["channels"]["0"]["role"] == "inactive"

    def test_data_channel(self):
        """Irregular transitions should be identified as data."""
        # Simulate irregular data pattern (~40% duty, many transitions)
        data_bits = []
        for i in range(500):
            data_bits.append(1 if (i % 5 < 2) else 0)
        raw = _build_samples({0: data_bits})
        result = analyze_channels(raw, 75_000_000, channels=[0])
        ch = result["channels"]["0"]
        assert ch["role"] == "data"
        assert ch["transitions"] > 2

    def test_duration_calculation(self):
        """Verify duration_us from sample count and rate."""
        raw = _build_samples({0: _repeat(0, 7500)})
        result = analyze_channels(raw, 75_000_000, channels=[0])
        # 7500 samples / 75 MHz = 100 us
        assert result["duration_us"] == 100.0
        assert result["sample_count"] == 7500


# ---------------------------------------------------------------------------
# TestIdentifyProtocol
# ---------------------------------------------------------------------------

class TestIdentifyProtocol:
    def test_spi_pattern(self):
        """Clock + CS + 2 data channels should identify as SPI."""
        analysis = {
            "channels": {
                "0": {"role": "clock", "frequency_hz": 1_000_000, "idle_state": "low", "duty_cycle": 0.5, "transitions": 200},
                "1": {"role": "cs", "frequency_hz": 100, "idle_state": "high", "duty_cycle": 0.9, "transitions": 4},
                "2": {"role": "data", "frequency_hz": 500_000, "idle_state": "low", "duty_cycle": 0.4, "transitions": 150},
                "3": {"role": "data", "frequency_hz": 500_000, "idle_state": "high", "duty_cycle": 0.6, "transitions": 120},
            }
        }
        candidates = identify_protocol(analysis)
        assert len(candidates) >= 1
        assert candidates[0]["protocol"] == "spi"
        assert candidates[0]["confidence"] == 0.9
        assert "clk" in candidates[0]["channels"]
        assert "mosi" in candidates[0]["channels"]
        assert "miso" in candidates[0]["channels"]

    def test_i2c_pattern(self):
        """2 channels both idle high, one clock-like should identify as I2C."""
        analysis = {
            "channels": {
                "4": {"role": "clock", "frequency_hz": 100_000, "idle_state": "high", "duty_cycle": 0.5, "transitions": 200},
                "5": {"role": "data", "frequency_hz": 80_000, "idle_state": "high", "duty_cycle": 0.6, "transitions": 150},
            }
        }
        candidates = identify_protocol(analysis)
        assert any(c["protocol"] == "i2c" for c in candidates)
        i2c = next(c for c in candidates if c["protocol"] == "i2c")
        assert i2c["channels"]["scl"] == 4
        assert i2c["channels"]["sda"] == 5

    def test_uart_pattern(self):
        """Single async data channel idle high, no clock should identify as UART."""
        analysis = {
            "channels": {
                "0": {"role": "inactive", "frequency_hz": 0, "idle_state": "low", "duty_cycle": 0, "transitions": 0},
                "3": {"role": "data", "frequency_hz": 55_000, "idle_state": "high", "duty_cycle": 0.65, "transitions": 80},
            }
        }
        candidates = identify_protocol(analysis)
        assert len(candidates) == 1
        assert candidates[0]["protocol"] == "uart"
        assert candidates[0]["estimated_baud"] == 115200

    def test_no_pattern(self):
        """All inactive channels should return empty candidates."""
        analysis = {
            "channels": {
                "0": {"role": "inactive", "frequency_hz": 0, "idle_state": "low", "duty_cycle": 0, "transitions": 0},
                "1": {"role": "inactive", "frequency_hz": 0, "idle_state": "low", "duty_cycle": 0, "transitions": 0},
            }
        }
        candidates = identify_protocol(analysis)
        assert candidates == []


# ---------------------------------------------------------------------------
# TestExtractSPIFrames
# ---------------------------------------------------------------------------

class TestExtractSPIFrames:
    def test_jedec_read(self):
        """Synthetic SPI JEDEC read: TX 0x9F, RX 0xEF 0x40 0x18."""
        sample_rate = 75_000_000
        # Use a 1 MHz clock: 75 samples per clock cycle (37 low, 38 high)
        half_clk = 37

        # Build the TX byte (0x9F) and RX bytes (0xEF, 0x40, 0x18) bit by bit
        tx_bits_val = [1, 0, 0, 1, 1, 1, 1, 1]  # 0x9F MSB first
        # During TX, MISO is don't-care (pad with 0)
        # Then 3 RX bytes on MISO, MOSI is don't-care (0)
        rx_bits_val = [
            1, 1, 1, 0, 1, 1, 1, 1,  # 0xEF
            0, 1, 0, 0, 0, 0, 0, 0,  # 0x40
            0, 0, 0, 1, 1, 0, 0, 0,  # 0x18
        ]

        total_bits = 8 + 24  # 32 clock cycles

        # Pre-frame idle: CS high, clock low, 100 samples
        idle = 100

        clk_samples = _repeat(0, idle)
        mosi_samples = _repeat(0, idle)
        miso_samples = _repeat(0, idle)
        cs_samples = _repeat(1, idle)

        # CS goes low
        cs_samples.append(0)
        clk_samples.append(0)
        mosi_samples.append(0)
        miso_samples.append(0)

        for bit_idx in range(total_bits):
            # MOSI: TX byte for first 8 bits, then 0
            mosi_val = tx_bits_val[bit_idx] if bit_idx < 8 else 0
            # MISO: 0 for first 8 bits, then RX bytes
            miso_val = rx_bits_val[bit_idx - 8] if bit_idx >= 8 else 0

            # Clock low phase: data setup
            for _ in range(half_clk):
                clk_samples.append(0)
                mosi_samples.append(mosi_val)
                miso_samples.append(miso_val)
                cs_samples.append(0)

            # Clock high phase: data sampled on rising edge
            for _ in range(half_clk + 1):
                clk_samples.append(1)
                mosi_samples.append(mosi_val)
                miso_samples.append(miso_val)
                cs_samples.append(0)

        # CS goes high (end frame) + idle
        for _ in range(idle):
            clk_samples.append(0)
            mosi_samples.append(0)
            miso_samples.append(0)
            cs_samples.append(1)

        raw = _build_samples({
            0: clk_samples,
            1: cs_samples,
            2: mosi_samples,
            3: miso_samples,
        })

        frames = extract_spi_frames(
            raw, sample_rate, clk_ch=0, mosi_ch=2, miso_ch=3, cs_ch=1
        )
        assert len(frames) == 1
        frame = frames[0]
        # TX: 0x9F followed by 3 zero bytes (MOSI during RX phase)
        assert frame["tx_hex"][:2] == "9f"
        assert frame["bits"] == 32
        # RX: first byte is 0x00 (MISO during TX phase), then EF 40 18
        assert frame["rx_hex"][2:] == "ef4018"


# ---------------------------------------------------------------------------
# TestExtractUARTData
# ---------------------------------------------------------------------------

class TestExtractUARTData:
    def test_decode_ascii_a(self):
        """Generate 115200 baud UART character 'A' (0x41) and decode it."""
        sample_rate = 75_000_000
        baud = 115200
        samples_per_bit = round(sample_rate / baud)  # ~651

        # 0x41 = 0b01000001, LSB first: 1,0,0,0,0,0,1,0
        data_bits = [1, 0, 0, 0, 0, 0, 1, 0]

        # Build signal: idle high, start bit (low), 8 data bits, stop bit (high), idle
        rx_samples = []
        # Idle
        rx_samples.extend(_repeat(1, samples_per_bit * 2))
        # Start bit
        rx_samples.extend(_repeat(0, samples_per_bit))
        # Data bits
        for b in data_bits:
            rx_samples.extend(_repeat(b, samples_per_bit))
        # Stop bit
        rx_samples.extend(_repeat(1, samples_per_bit))
        # Idle
        rx_samples.extend(_repeat(1, samples_per_bit * 2))

        raw = _build_samples({0: rx_samples})
        result = extract_uart_data(raw, sample_rate, rx_ch=0, baud=baud)
        assert result["bytes"] == [0x41]
        assert result["text"] == "A"
        assert result["baud"] == baud

    def test_auto_baud_detection(self):
        """Generate character at known baud, verify auto-detected baud matches."""
        sample_rate = 75_000_000
        baud = 9600
        samples_per_bit = round(sample_rate / baud)  # 7812

        # 0x55 = 0b01010101, LSB first: 1,0,1,0,1,0,1,0 -- nice alternating pattern
        data_bits = [1, 0, 1, 0, 1, 0, 1, 0]

        rx_samples = []
        rx_samples.extend(_repeat(1, samples_per_bit * 2))
        rx_samples.extend(_repeat(0, samples_per_bit))  # start
        for b in data_bits:
            rx_samples.extend(_repeat(b, samples_per_bit))
        rx_samples.extend(_repeat(1, samples_per_bit))  # stop
        rx_samples.extend(_repeat(1, samples_per_bit * 2))

        raw = _build_samples({0: rx_samples})
        result = extract_uart_data(raw, sample_rate, rx_ch=0, baud=None)
        assert result["baud"] == baud
        assert result["bytes"] == [0x55]


# ---------------------------------------------------------------------------
# TestExtractI2CFrames
# ---------------------------------------------------------------------------

class TestExtractI2CFrames:
    def test_basic_write(self):
        """Generate I2C write to 0x50 with one data byte 0xAB."""
        sample_rate = 75_000_000
        # Use 100 kHz I2C: 750 samples per clock cycle
        half_clk = 375

        # Address byte: 0x50 = 0b1010000, R/W=0 (write) -> 0xA0 on bus
        # Bits MSB first: 1,0,1,0,0,0,0 (addr) + 0 (W)
        addr_bits = [1, 0, 1, 0, 0, 0, 0, 0]
        addr_ack = 0  # ACK

        # Data byte: 0xAB = 0b10101011 MSB first
        data_bits = [1, 0, 1, 0, 1, 0, 1, 1]
        data_ack = 0  # ACK

        all_bits = addr_bits + [addr_ack] + data_bits + [data_ack]

        # Build SDA and SCL waveforms
        sda_samples = []
        scl_samples = []

        # Idle: both high
        idle_len = 200
        sda_samples.extend(_repeat(1, idle_len))
        scl_samples.extend(_repeat(1, idle_len))

        # START: SDA falls while SCL high
        sda_samples.extend(_repeat(0, half_clk))
        scl_samples.extend(_repeat(1, half_clk))

        # Clock each bit: SCL low (SDA changes), SCL high (SDA sampled)
        for bit_val in all_bits:
            # SCL low: SDA sets up
            sda_samples.extend(_repeat(bit_val, half_clk))
            scl_samples.extend(_repeat(0, half_clk))
            # SCL high: data sampled on rising edge
            sda_samples.extend(_repeat(bit_val, half_clk))
            scl_samples.extend(_repeat(1, half_clk))

        # STOP: SCL high, SDA rises
        sda_samples.extend(_repeat(0, half_clk // 2))
        scl_samples.extend(_repeat(1, half_clk // 2))
        sda_samples.extend(_repeat(1, half_clk))
        scl_samples.extend(_repeat(1, half_clk))

        # Idle
        sda_samples.extend(_repeat(1, idle_len))
        scl_samples.extend(_repeat(1, idle_len))

        raw = _build_samples({0: sda_samples, 1: scl_samples})
        frames = extract_i2c_frames(raw, sample_rate, sda_ch=0, scl_ch=1)

        assert len(frames) == 1
        frame = frames[0]
        assert frame["addr"] == "0x50"
        assert frame["addr_7bit"] == 0x50
        assert frame["rw"] == "W"
        assert frame["ack"] is True
        assert frame["data_hex"] == "ab"


# ---------------------------------------------------------------------------
# TestCountTransitions
# ---------------------------------------------------------------------------

class TestCountTransitions:
    def test_no_transitions(self):
        assert count_transitions([0, 0, 0, 0]) == 0

    def test_alternating(self):
        assert count_transitions([0, 1, 0, 1, 0]) == 4

    def test_single_transition(self):
        assert count_transitions([0, 0, 1, 1]) == 1
