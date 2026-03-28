# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

buspirate-mcp is an MCP server that wraps the BusPirate 6's BPIO2 binary protocol SDK, exposing UART, power supply, GPIO, and flash-dump operations as MCP tools over stdio transport.

## Architecture

```
MCP client (Claude Code, etc.)
  |
  stdio transport
  |
buspirate-mcp (server.py)
  |
  tools.py -> hardware.py -> BPIO2 SDK
  |
BusPirate 6 -> Target Device
```

Only `hardware.py` imports from the BPIO2 SDK. Everything else talks to `hardware.py`. This makes mocking trivial for tests.

## Safety Model

Three tiers enforced at the MCP server boundary:

- **read-only**: full autonomy (list_devices, verify_connection, scan_baud, read_output, spi_probe, spi_read, spi_dump, spi_transfer, i2c_scan, i2c_read, i2c_dump, onewire_search, onewire_read)
- **allowed-write**: autonomous but logged (open_uart, send_command, close_uart, enter_download_mode, read_flash, open_spi, close_spi, open_i2c, close_i2c, open_1wire, close_1wire)
- **approval-write**: blocks until human confirms via _confirmed token (set_voltage, set_power, spi_write, i2c_write)

Wrong voltage fries chips. Writing SPI flash or I2C devices overwrites data irreversibly. Do not bypass or weaken the approval-write gate.

## BusPirate 6 Hardware Notes

### BPIO2 binary mode

The BP6 must have BPIO2 binary mode enabled before the SDK can talk to it. Enable via the terminal port (ACM0): `binmode` -> select `2` (BPIO2 flatbuffer interface) -> `y` to save. This persists across reboots.

### Pin mapping (UART)

BP6 UART mode uses IO4 (TX->) and IO5 (RX<-). The BP display is the authoritative source for pin assignment.

**Beware:** `status_request()` returns a `mode_pin_labels` array that includes VOUT at index 0, shifting all labels by one. Index 5 in the labels array = physical IO4, index 6 = IO5. Trust the display, not the raw array indices.

### SPI Mode

- Pin mapping: IO2=CLK, IO3=MISO, IO4=MOSI, IO5=CS
- Default speed: 1 MHz, configurable up to 12 MHz for flash dumps
- SPI flash commands: JEDEC ID (0x9F), Read (0x03), Write Enable (0x06), Page Program (0x02), Chip Erase (0xC7)
- Flash dumps use 512-byte chunks to avoid buffer overruns

### I2C Mode

- Pin mapping: IO4=SDA, IO5=SCL
- Default speed: 400 kHz (fast mode), configurable
- Pullups enabled by default (required for I2C)
- SDK scan() has print() calls -- hardware.py redirects stdout to prevent MCP stdio corruption

### 1-Wire Mode

- Pin mapping: IO4=DQ (data line)
- Pullup enabled by default (required for 1-Wire)
- Read ROM (0x33) supports single-device identification. Multi-device search not implemented in SDK.
- CRC-8 validation (Dallas polynomial 0x8C) on ROM codes

### Power supply

- Use `set_psu_enable(voltage_mv, current_ma)` to power on
- Use `set_psu_disable()` to power off -- `set_psu_enable(voltage_mv=0)` does NOT disable the PSU
- Power cycle the target between configuration changes; the BP6 may hold stale UART state otherwise
- If the BP6 becomes unresponsive (stale connection, timeout on status_request), a USB replug is the most reliable recovery

### GPIO for download mode

Free IO pins (IO0-IO3, IO6-IO7) can be used for GPIO control while UART mode is active on IO4/IO5. Use `set_pin_output()` and `release_pin()` in hardware.py. This enables automated bootloader entry (hold boot pin low, pulse reset).

**Important:** BPIO2 (ACM1) and terminal bridge mode (ACM0) cannot operate simultaneously. The firmware locks up. Use BPIO2 for GPIO setup, then switch to terminal for bridge mode.

### Flash dumping via UART bridge

The BP6 `bridge` command creates a transparent serial passthrough on ACM0. esptool.py can read/write flash through this bridge using `--before no-reset --after no-reset --no-stub`. Baud rates above 460800 risk buffer overruns in bridge mode.

**After a flash dump, bridge mode stays active.** It cannot be exited programmatically. Press the physical button on the BP6 or USB-replug to exit bridge and restore normal BPIO2 operation.

### USB CDC ports

The BP6 exposes two USB CDC serial ports:
- ACM0: text terminal (VT100), also used for UART bridge mode
- ACM1: binary protocol (BPIO2)

The binary port number may change after USB replug (ACM1 -> ACM2 etc). The MCP server auto-detects the correct port.

User must be in the `dialout` group for serial access. A new shell or re-login is required after `usermod -aG dialout $USER`.

## Build and Run

```bash
# First time: clone submodules
git submodule update --init

# Install
pip install -e ".[dev]"

# Run server (stdio transport, spawned by MCP client)
python -m buspirate_mcp

# Tests (no hardware needed, 77 tests)
pytest

# Integration tests (BusPirate must be connected)
pytest tests/ -m hardware
```

## Style

- Python 3.11+
- No emojis, no em-dashes in code, comments, commits, or docs
- Commit messages: short, to the point
