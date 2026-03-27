"""MCP tool implementations for BusPirate UART operations.

Each function is an async tool handler. Registration with the MCP
server happens in server.py. These functions contain the logic only.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from buspirate_mcp.hardware import BusPirateHardware
from buspirate_mcp.safety import validate_voltage_range
from buspirate_mcp.session import SessionManager

COMMON_BAUD_RATES = [115200, 57600, 38400, 19200, 9600, 4800, 2400, 1200]
PROMPT_PATTERN = re.compile(r"^\s*[#$>\]]", re.MULTILINE)
BAUD_SCAN_READ_TIME_S = 0.5
SILENCE_THRESHOLD_S = 0.5


def _ascii_score(data: bytes) -> float:
    """Score data by ratio of printable ASCII to total bytes."""
    if not data:
        return 0.0
    printable = sum(
        1 for b in data if 32 <= b <= 126 or b in (9, 10, 13)
    )
    return printable / len(data)


async def tool_list_devices() -> dict[str, Any]:
    """List BusPirate devices on USB."""
    devices = BusPirateHardware.list_devices()
    result: dict[str, Any] = {"devices": devices}
    if not devices:
        result["hint"] = (
            "No BusPirate found. Check USB cable, run lsusb, "
            "verify permissions on /dev/ttyACM*."
        )
    return result


async def tool_verify_connection(
    hardware: Any,
    pins: dict[str, int],
    sample_duration_ms: int = 2000,
) -> dict[str, Any]:
    """Check for signal activity on the specified pins."""
    tx_pin = pins["tx"]

    # Ensure UART mode is active before reading pins
    hardware.configure_uart(speed=115200)

    samples = []
    transitions = 0
    start = time.monotonic()
    end_time = start + (sample_duration_ms / 1000.0)

    while time.monotonic() < end_time:
        voltages = hardware.get_pin_voltages()
        if voltages is None:
            await asyncio.sleep(0.05)
            continue
        samples.append(voltages[tx_pin])
        if len(samples) >= 2 and samples[-1] != samples[-2]:
            transitions += 1
        await asyncio.sleep(0.05)

    if not samples:
        return {
            "activity_detected": False,
            "transitions": 0,
            "approx_frequency_hz": 0.0,
            "samples_taken": 0,
            "voltage_range_mv": [0, 0],
            "message": (
                f"Could not read pin voltages on pin {tx_pin}. "
                f"UART mode may not be active or hardware not responding."
            ),
        }

    activity = transitions > 0
    duration_s = time.monotonic() - start
    freq_hz = round(transitions / duration_s, 1) if activity else 0.0

    return {
        "activity_detected": activity,
        "transitions": transitions,
        "approx_frequency_hz": freq_hz,
        "samples_taken": len(samples),
        "voltage_range_mv": [min(samples), max(samples)],
        "message": (
            f"Signal transitions detected on pin {tx_pin} "
            f"({transitions} transitions, ~{freq_hz} Hz)."
            if activity
            else f"No activity on pin {tx_pin}. Target may be "
            f"quiescent, powered off, or wiring incorrect."
        ),
    }


async def tool_scan_baud(
    hardware: Any,
) -> dict[str, Any]:
    """Scan common baud rates and score by readable ASCII output."""
    candidates = []

    for rate in COMMON_BAUD_RATES:
        hardware.configure_uart(speed=rate)
        hardware.read()  # flush stale bytes from previous rate
        await asyncio.sleep(BAUD_SCAN_READ_TIME_S)
        data = hardware.read() or b""
        score = _ascii_score(data)
        candidates.append({
            "baud": rate,
            "score": round(score, 3),
            "sample_bytes": len(data),
            "preview": data[:64].decode("utf-8", errors="replace"),
        })

    candidates.sort(key=lambda c: c["score"], reverse=True)
    best = candidates[0] if candidates else {"score": 0.0}

    return {
        "candidates": candidates,
        "recommended": candidates[0]["baud"] if best["score"] >= 0.7 else None,
        "best_score": best["score"],
        "message": (
            f"Recommended baud rate: {candidates[0]['baud']} "
            f"(confidence: {best['score']:.1%})"
            if best["score"] >= 0.7
            else "No baud rate scored above 0.7 threshold. "
            "Provide all scores for user decision."
        ),
    }


async def tool_open_uart(
    session_manager: SessionManager,
    hardware: Any,
    baud: int,
    pins: dict[str, str],
    engagement_name: str,
    device_path: str = "",
    project_path: str | None = None,
) -> dict[str, Any]:
    """Open a persistent UART session and start logging."""
    hardware.configure_uart(speed=baud)
    session = session_manager.create(
        name=engagement_name,
        hardware=hardware,
        baud=baud,
        pins=pins,
        device_path=device_path,
        project_path=project_path,
    )
    return {
        "session_id": session.session_id,
        "engagement_path": str(session.engagement_path),
        "baud": baud,
        "pins": pins,
    }


async def tool_read_output(
    session_manager: SessionManager,
    session_id: str,
    timeout_ms: int = 5000,
) -> dict[str, Any]:
    """Read available data from an open UART session."""
    session = session_manager.get(session_id)
    collected = b""
    end_time = time.monotonic() + (timeout_ms / 1000.0)

    while time.monotonic() < end_time:
        chunk = session.hardware.read() or b""
        if chunk:
            collected += chunk
            session.log_rx(chunk)
            if PROMPT_PATTERN.search(
                collected.decode("utf-8", errors="replace")
            ):
                break
        await asyncio.sleep(0.05)  # always yield to event loop

    text = collected.decode("utf-8", errors="replace")
    return {
        "text": text,
        "bytes_received": len(collected),
    }


async def tool_send_command(
    session_manager: SessionManager,
    session_id: str,
    command: str,
    timeout_ms: int = 3000,
) -> dict[str, Any]:
    """Send a command and capture the response."""
    session = session_manager.get(session_id)
    cmd_bytes = (command + "\r\n").encode("utf-8")
    session.hardware.write(cmd_bytes)
    session.log_tx(cmd_bytes)

    collected = b""
    last_rx_time = time.monotonic()
    end_time = time.monotonic() + (timeout_ms / 1000.0)

    while time.monotonic() < end_time:
        chunk = session.hardware.read() or b""
        if chunk:
            collected += chunk
            session.log_rx(chunk)
            last_rx_time = time.monotonic()

            decoded = collected.decode("utf-8", errors="replace")
            if PROMPT_PATTERN.search(decoded):
                break
        elif collected and (time.monotonic() - last_rx_time) > SILENCE_THRESHOLD_S:
            break
        await asyncio.sleep(0.05)  # always yield to event loop

    response = collected.decode("utf-8", errors="replace")
    return {
        "response": response,
        "bytes_received": len(collected),
    }


async def tool_close_uart(
    session_manager: SessionManager,
    session_id: str,
) -> dict[str, Any]:
    """Close a UART session and finalize logs."""
    session_manager.close(session_id)
    return {"closed": True, "session_id": session_id}


async def tool_set_voltage(
    hardware: Any,
    voltage_v: float,
    current_limit_ma: int,
) -> dict[str, Any]:
    """Set power supply voltage. Validates range before applying."""
    try:
        validate_voltage_range(voltage_v, current_limit_ma)
    except ValueError as e:
        return {"applied": False, "error": str(e)}

    result = hardware.set_voltage(voltage_v, current_limit_ma)
    return {
        "applied": result,
        "voltage_v": voltage_v,
        "current_limit_ma": current_limit_ma,
        "error": None,
    }


async def tool_set_power(
    hardware: Any,
    enable: bool,
) -> dict[str, Any]:
    """Enable or disable power supply."""
    result = hardware.set_power(enable)
    return {
        "applied": result,
        "power": "on" if enable else "off",
    }


async def tool_enter_download_mode(
    hardware: Any,
    boot_pin: int,
    reset_pin: int,
) -> dict[str, Any]:
    """Put target into UART download mode via GPIO pin toggling.

    Holds boot_pin LOW, pulses reset_pin LOW then HIGH, then releases
    boot_pin. Works for ESP32, ESP8266, and other chips with UART
    bootloaders triggered by a strapping pin.
    """
    # Hold boot select pin LOW
    hardware.set_pin_output(boot_pin, high=False)
    await asyncio.sleep(0.3)

    # Pulse reset: LOW then HIGH
    hardware.set_pin_output(reset_pin, high=False)
    await asyncio.sleep(0.2)
    hardware.set_pin_output(reset_pin, high=True)
    await asyncio.sleep(0.5)

    # Release boot pin
    hardware.release_pin(boot_pin)
    hardware.release_pin(reset_pin)

    return {
        "status": "download_mode_entered",
        "boot_pin": boot_pin,
        "reset_pin": reset_pin,
        "message": (
            f"Boot pin IO{boot_pin} toggled, reset pin IO{reset_pin} pulsed. "
            f"Target should be in UART download mode."
        ),
    }


def _enter_bridge_mode(terminal_port: str) -> bool:
    """Enter UART bridge mode on the BP6 terminal port.

    The bridge command makes ACM0 a transparent serial passthrough
    to the target. Handles VT100 prompt, detects current mode, and
    enters bridge. Returns True on success.
    """
    import serial
    ser = serial.Serial(terminal_port, 115200, timeout=2)
    time.sleep(0.5)
    ser.read(ser.in_waiting or 1)

    # Send enter to see what prompt we get
    ser.write(b'\r')
    time.sleep(0.5)
    resp = ser.read(ser.in_waiting or 1).decode('utf-8', errors='replace')

    # Handle VT100 prompt (appears after fresh USB plug)
    if 'VT100' in resp or resp.strip() == '':
        ser.write(b'n\r')
        time.sleep(0.5)
        resp = ser.read(ser.in_waiting or 1).decode('utf-8', errors='replace')

    # Check if we're already in UART mode
    if 'UART>' in resp or 'UART>' in resp.upper():
        # Already in UART mode, send bridge
        ser.write(b'bridge\r')
        time.sleep(1)
        resp = ser.read(ser.in_waiting or 1).decode('utf-8', errors='replace')
        ser.close()
        return 'bridge' in resp.lower()

    if 'HiZ>' in resp:
        # Need to enter UART mode first
        ser.write(b'm\r')
        time.sleep(0.5)
        ser.read(ser.in_waiting or 1)
        ser.write(b'3\r')
        time.sleep(1)
        ser.read(ser.in_waiting or 1)
        ser.write(b'y\r')
        time.sleep(0.5)
        ser.read(ser.in_waiting or 1)

    # Try bridge
    ser.write(b'bridge\r')
    time.sleep(1)
    resp = ser.read(ser.in_waiting or 1).decode('utf-8', errors='replace')
    ser.close()
    return 'bridge' in resp.lower()


def _exit_bridge_mode(terminal_port: str) -> None:
    """Attempt to exit UART bridge mode on the BP6.

    Bridge mode exits when the BP6 button is pressed, or when the
    serial port is closed with a break condition. We send a serial
    break and toggle DTR/RTS to signal the BP6 to drop bridge mode.
    """
    import serial
    try:
        ser = serial.Serial(terminal_port, 115200, timeout=1)
        ser.send_break(duration=0.5)
        ser.dtr = False
        ser.rts = False
        time.sleep(0.3)
        ser.dtr = True
        ser.rts = True
        time.sleep(0.3)
        ser.close()
    except Exception:
        pass  # best effort


async def tool_read_flash(
    hardware: Any,
    terminal_port: str,
    offset: int,
    size: int,
    output_path: str,
    boot_pin: int = -1,
    reset_pin: int = -1,
    baud: int = 115200,
) -> dict[str, Any]:
    """Read flash memory from target via esptool through BP6 UART bridge.

    Optionally enters download mode first if boot_pin and reset_pin are
    provided. Then enters bridge mode on the terminal port and runs
    esptool read-flash.
    """
    # Step 1: Enter download mode if pins provided
    if boot_pin >= 0 and reset_pin >= 0:
        dl_result = await tool_enter_download_mode(
            hardware, boot_pin, reset_pin,
        )
        if dl_result["status"] != "download_mode_entered":
            return {"error": "Failed to enter download mode", "detail": dl_result}

    # Step 2: Enter bridge mode.
    # If bridge is already active from a previous read_flash call,
    # _enter_bridge_mode will send 'bridge\r' which the target ignores.
    # esptool will re-sync either way.
    _enter_bridge_mode(terminal_port)

    # Step 3: Run esptool
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Find esptool in the same venv as the MCP server
    esptool_bin = shutil.which('esptool')
    if not esptool_bin:
        # Fall back to looking next to the Python interpreter
        esptool_bin = str(Path(sys.executable).parent / 'esptool')
    cmd = [
        esptool_bin, '--port', terminal_port,
        '--before', 'no-reset', '--after', 'no-reset',
        '--no-stub', '--baud', str(baud),
        'read-flash', str(offset), str(size), str(output),
    ]

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=max(60, size // 1000),  # ~1s per KB at 115200 + margin
        )
    except FileNotFoundError:
        return {
            "error": "esptool not found. Install with: pip install esptool",
        }
    except subprocess.TimeoutExpired:
        return {
            "error": "esptool timed out",
            "command": ' '.join(cmd),
        }

    if result.returncode != 0:
        return {
            "error": "esptool failed",
            "returncode": result.returncode,
            "stderr": result.stderr[-500:],
            "command": ' '.join(cmd),
        }

    file_size = output.stat().st_size if output.exists() else 0

    # Step 4: Attempt cleanup.
    # Bridge mode on ACM0 cannot be exited programmatically (only via
    # the BP6 physical button). BPIO2 commands hang while bridge is
    # active. The user must USB-replug the BP6 after flash operations
    # to restore normal operation.
    # TODO: find a way to exit bridge mode without physical button

    return {
        "status": "success",
        "output_path": str(output),
        "bytes_read": file_size,
        "offset": offset,
        "size_requested": size,
        "baud": baud,
        "stdout": result.stdout[-300:],
        "cleanup": (
            "Bridge mode is still active. Press the BP6 button or "
            "USB-replug to exit bridge mode and restore normal operation."
        ),
    }
