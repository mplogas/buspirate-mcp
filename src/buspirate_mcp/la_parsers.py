"""Signal analysis and protocol identification for FALA captures.

Raw samples are 1 byte per sample, each bit represents one IO channel
(IO0=bit0 through IO7=bit7). Sample rate is typically 75 MHz (8x oversample).
"""

from __future__ import annotations
from typing import Any


def parse_fala_notification(line: str) -> dict[str, Any]:
    """Parse '$FALADATA;8;0;0;N;75000000;7468;0;' into structured dict.

    Returns dict with channels, trigger_pin, trigger_mask, edge_trigger,
    sample_rate_hz, samples, pre_samples. Returns {"error": ...} on invalid input.
    """
    line = line.strip().rstrip(";")
    parts = line.split(";")
    if len(parts) < 8 or parts[0] != "$FALADATA":
        return {"error": f"Invalid FALA notification: {line}"}
    try:
        return {
            "channels": int(parts[1]),
            "trigger_pin": int(parts[2]),
            "trigger_mask": int(parts[3]),
            "edge_trigger": parts[4] == "Y",
            "sample_rate_hz": int(parts[5]),
            "samples": int(parts[6]),
            "pre_samples": int(parts[7]),
        }
    except (ValueError, IndexError) as exc:
        return {"error": f"Failed to parse notification: {exc}"}


def extract_channel(raw: bytes, channel: int) -> list[int]:
    """Extract a single channel's bit values from raw samples.

    Returns list of 0/1 values, one per sample.
    """
    return [(b >> channel) & 1 for b in raw]


def count_transitions(bits: list[int]) -> int:
    """Count the number of transitions (0->1 or 1->0) in a bit sequence."""
    return sum(1 for i in range(1, len(bits)) if bits[i] != bits[i - 1])


def analyze_channels(
    raw: bytes,
    sample_rate_hz: int,
    channels: list[int] | None = None,
) -> dict[str, Any]:
    """Analyze each channel for signal characteristics.

    Returns per-channel dict with transitions, frequency_hz, duty_cycle,
    idle_state, and role guess (clock, cs, data, inactive).
    """
    if channels is None:
        channels = list(range(8))

    duration_s = len(raw) / sample_rate_hz if sample_rate_hz > 0 else 0
    result = {}

    for ch in channels:
        bits = extract_channel(raw, ch)
        if not bits:
            continue

        transitions = count_transitions(bits)
        high_count = sum(bits)
        total = len(bits)
        duty_cycle = high_count / total if total > 0 else 0
        idle_state = "high" if bits[0] == 1 else "low"

        # Frequency: each full cycle is 2 transitions (rising + falling)
        frequency_hz = 0
        if transitions >= 2 and duration_s > 0:
            frequency_hz = round(transitions / (2 * duration_s))

        # Role guess heuristics
        role = "inactive"
        if transitions == 0:
            role = "inactive"
        elif 0.45 <= duty_cycle <= 0.55 and transitions > 10:
            role = "clock"
        elif idle_state == "high" and duty_cycle > 0.7 and transitions < 20:
            role = "cs"  # chip select: mostly high, brief active-low
        elif transitions > 2:
            role = "data"

        result[str(ch)] = {
            "transitions": transitions,
            "frequency_hz": frequency_hz,
            "duty_cycle": round(duty_cycle, 3),
            "idle_state": idle_state,
            "role": role,
        }

    return {
        "channels": result,
        "sample_count": len(raw),
        "sample_rate_hz": sample_rate_hz,
        "duration_us": round(duration_s * 1_000_000, 2),
    }


def identify_protocol(analysis: dict[str, Any]) -> list[dict[str, Any]]:
    """Heuristic protocol identification from channel analysis.

    Returns ranked list of candidates with protocol, confidence, and channel mapping.
    """
    channels = analysis.get("channels", {})
    candidates = []

    # Find channels by role
    clocks = [ch for ch, info in channels.items() if info["role"] == "clock"]
    cs_pins = [ch for ch, info in channels.items() if info["role"] == "cs"]
    data_pins = [ch for ch, info in channels.items() if info["role"] == "data"]
    active_pins = [ch for ch, info in channels.items() if info["role"] != "inactive"]

    # SPI: clock + CS + 1-2 data lines
    if clocks and cs_pins and data_pins:
        confidence = 0.9
        mapping = {"clk": int(clocks[0]), "cs": int(cs_pins[0])}
        if len(data_pins) >= 2:
            mapping["mosi"] = int(data_pins[0])
            mapping["miso"] = int(data_pins[1])
        elif len(data_pins) == 1:
            mapping["mosi"] = int(data_pins[0])
            confidence = 0.7
        candidates.append({
            "protocol": "spi",
            "confidence": confidence,
            "channels": mapping,
            "clock_hz": channels[clocks[0]]["frequency_hz"],
        })

    # I2C: exactly 2 active channels, both idle high, one clock-like
    if len(active_pins) == 2:
        ch_a = channels[active_pins[0]]
        ch_b = channels[active_pins[1]]
        if ch_a["idle_state"] == "high" and ch_b["idle_state"] == "high":
            # The one with more consistent frequency is SCL
            if ch_a["role"] == "clock":
                scl, sda = active_pins[0], active_pins[1]
            elif ch_b["role"] == "clock":
                scl, sda = active_pins[1], active_pins[0]
            else:
                # Pick the one with higher frequency as clock
                if ch_a["frequency_hz"] > ch_b["frequency_hz"]:
                    scl, sda = active_pins[0], active_pins[1]
                else:
                    scl, sda = active_pins[1], active_pins[0]
            candidates.append({
                "protocol": "i2c",
                "confidence": 0.7,
                "channels": {"scl": int(scl), "sda": int(sda)},
                "clock_hz": channels[scl]["frequency_hz"],
            })

    # UART: single async data channel, no clock
    if not clocks and len(data_pins) == 1:
        ch_info = channels[data_pins[0]]
        if ch_info["idle_state"] == "high":
            # Estimate baud from transition timing
            baud = _estimate_baud(ch_info["frequency_hz"])
            candidates.append({
                "protocol": "uart",
                "confidence": 0.6,
                "channels": {"rx": int(data_pins[0])},
                "estimated_baud": baud,
            })

    # Sort by confidence descending
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    return candidates


def _estimate_baud(frequency_hz: int) -> int:
    """Estimate UART baud rate from signal frequency.

    UART frequency is roughly baud_rate / 2 for random data
    (each bit is either 0 or 1, so on average transitions every 2 bits).
    Match to nearest standard baud rate.
    """
    standard_bauds = [115200, 57600, 38400, 19200, 9600, 4800, 2400, 1200]
    if frequency_hz == 0:
        return 0
    # Rough estimate: frequency * 2 is in the neighborhood of baud
    estimated = frequency_hz * 2
    # Find nearest standard baud
    return min(standard_bauds, key=lambda b: abs(b - estimated))


def extract_spi_frames(
    raw: bytes,
    sample_rate_hz: int,
    clk_ch: int,
    mosi_ch: int,
    miso_ch: int,
    cs_ch: int,
) -> list[dict[str, Any]]:
    """Decode SPI frames from raw samples.

    Finds CS active-low regions, samples MOSI/MISO on CLK rising edges.
    Returns list of frames with tx_hex, rx_hex, duration_us.
    """
    clk = extract_channel(raw, clk_ch)
    mosi = extract_channel(raw, mosi_ch)
    miso = extract_channel(raw, miso_ch)
    cs = extract_channel(raw, cs_ch)

    frames = []
    in_frame = False
    frame_start = 0
    mosi_bits: list[int] = []
    miso_bits: list[int] = []

    for i in range(1, len(raw)):
        cs_active = cs[i] == 0  # active low

        if cs_active and not in_frame:
            # CS just went active
            in_frame = True
            frame_start = i
            mosi_bits = []
            miso_bits = []
        elif not cs_active and in_frame:
            # CS just went inactive -- frame complete
            in_frame = False
            if mosi_bits:
                tx_bytes = _bits_to_bytes(mosi_bits)
                rx_bytes = _bits_to_bytes(miso_bits)
                duration_us = (i - frame_start) / sample_rate_hz * 1_000_000
                frames.append({
                    "tx_hex": tx_bytes.hex(),
                    "rx_hex": rx_bytes.hex(),
                    "bits": len(mosi_bits),
                    "duration_us": round(duration_us, 2),
                })

        # Sample on CLK rising edge while CS active
        if in_frame and clk[i] == 1 and clk[i - 1] == 0:
            mosi_bits.append(mosi[i])
            miso_bits.append(miso[i])

    # Handle frame still open at end of capture
    if in_frame and mosi_bits:
        tx_bytes = _bits_to_bytes(mosi_bits)
        rx_bytes = _bits_to_bytes(miso_bits)
        duration_us = (len(raw) - frame_start) / sample_rate_hz * 1_000_000
        frames.append({
            "tx_hex": tx_bytes.hex(),
            "rx_hex": rx_bytes.hex(),
            "bits": len(mosi_bits),
            "duration_us": round(duration_us, 2),
        })

    return frames


def extract_uart_data(
    raw: bytes,
    sample_rate_hz: int,
    rx_ch: int,
    baud: int | None = None,
) -> dict[str, Any]:
    """Decode UART data from raw samples. Auto-detect baud if not provided.

    Standard UART: idle high, start bit (low), 8 data bits LSB first, stop bit (high).
    """
    bits = extract_channel(raw, rx_ch)
    if not bits:
        return {"baud": 0, "data_hex": "", "text": "", "bytes": []}

    # Auto-detect baud from shortest pulse width
    if baud is None:
        baud = _detect_uart_baud(bits, sample_rate_hz)
        if baud == 0:
            return {
                "baud": 0,
                "data_hex": "",
                "text": "",
                "bytes": [],
                "error": "Could not detect baud rate",
            }

    samples_per_bit = sample_rate_hz / baud
    decoded_bytes = []
    i = 0

    while i < len(bits) - int(samples_per_bit * 10):
        # Look for start bit (high -> low transition)
        if bits[i] == 1 and i + 1 < len(bits) and bits[i + 1] == 0:
            # Start bit found at i+1
            # Sample each data bit at center
            byte_val = 0
            start = i + 1
            for bit_num in range(8):
                sample_pos = int(start + samples_per_bit * (bit_num + 1.5))
                if sample_pos < len(bits):
                    byte_val |= bits[sample_pos] << bit_num  # LSB first
            decoded_bytes.append(byte_val)
            # Skip past this byte (start + 8 data + stop = 10 bit periods)
            i = int(start + samples_per_bit * 10)
        else:
            i += 1

    data = bytes(decoded_bytes)
    return {
        "baud": baud,
        "data_hex": data.hex(),
        "text": data.decode("utf-8", errors="replace"),
        "bytes": decoded_bytes,
    }


def extract_i2c_frames(
    raw: bytes,
    sample_rate_hz: int,
    sda_ch: int,
    scl_ch: int,
) -> list[dict[str, Any]]:
    """Decode I2C frames from raw samples.

    START: SDA falls while SCL high. STOP: SDA rises while SCL high.
    Data sampled on SCL rising edge.
    """
    sda = extract_channel(raw, sda_ch)
    scl = extract_channel(raw, scl_ch)
    frames = []

    in_frame = False
    current_bits: list[int] = []
    frame_start = 0

    for i in range(1, len(raw)):
        scl_high = scl[i] == 1
        sda_fell = sda[i] == 0 and sda[i - 1] == 1
        sda_rose = sda[i] == 1 and sda[i - 1] == 0
        scl_rose = scl[i] == 1 and scl[i - 1] == 0

        # START condition: SDA falls while SCL high
        if sda_fell and scl_high:
            in_frame = True
            frame_start = i
            current_bits = []
            continue

        # STOP condition: SDA rises while SCL high
        if sda_rose and scl_high and in_frame:
            in_frame = False
            if len(current_bits) >= 9:
                frame = _parse_i2c_bits(
                    current_bits, frame_start, i, sample_rate_hz
                )
                frames.append(frame)
            current_bits = []
            continue

        # Data: sample SDA on SCL rising edge
        if scl_rose and in_frame:
            current_bits.append(sda[i])

    return frames


def _parse_i2c_bits(
    bits: list[int], start: int, end: int, sample_rate_hz: int
) -> dict:
    """Parse collected I2C bits into address, data, and ACK/NACK."""
    # First 8 bits: 7-bit address + R/W
    addr_bits = bits[:7]
    rw_bit = bits[7] if len(bits) > 7 else 0
    ack = bits[8] if len(bits) > 8 else 1  # 0=ACK, 1=NACK

    addr = 0
    for bit in addr_bits:
        addr = (addr << 1) | bit

    # Remaining bits are data bytes (8 bits + 1 ACK each)
    data_bytes = []
    pos = 9
    while pos + 8 < len(bits):
        byte_val = 0
        for j in range(8):
            byte_val = (byte_val << 1) | bits[pos + j]
        data_bytes.append(byte_val)
        # ACK bit at pos+8
        pos += 9

    duration_us = (end - start) / sample_rate_hz * 1_000_000
    return {
        "addr": f"0x{addr:02X}",
        "addr_7bit": addr,
        "rw": "R" if rw_bit else "W",
        "ack": ack == 0,
        "data_hex": bytes(data_bytes).hex() if data_bytes else "",
        "duration_us": round(duration_us, 2),
    }


def _bits_to_bytes(bits: list[int]) -> bytes:
    """Convert a list of bits (MSB first) to bytes."""
    result = []
    for i in range(0, len(bits), 8):
        byte_val = 0
        for j in range(min(8, len(bits) - i)):
            byte_val = (byte_val << 1) | bits[i + j]
        result.append(byte_val)
    return bytes(result)


def _detect_uart_baud(bits: list[int], sample_rate_hz: int) -> int:
    """Detect UART baud rate from shortest pulse width."""
    min_pulse = float("inf")
    count = 0
    for i in range(1, len(bits)):
        if bits[i] != bits[i - 1]:
            if count > 0 and count < min_pulse:
                min_pulse = count
            count = 0
        count += 1

    if min_pulse == float("inf") or min_pulse == 0:
        return 0

    # Baud = sample_rate / samples_per_bit
    estimated_baud = sample_rate_hz / min_pulse
    # Match to nearest standard
    standard_bauds = [115200, 57600, 38400, 19200, 9600, 4800, 2400, 1200]
    return min(standard_bauds, key=lambda b: abs(b - estimated_baud))
