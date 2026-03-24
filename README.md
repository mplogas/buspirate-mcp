# buspirate-mcp

MCP server for BusPirate 6 hardware security testing. Exposes UART, power supply, GPIO, and flash-dump operations as [Model Context Protocol](https://modelcontextprotocol.io/) tools over stdio transport.

Built for use with Claude Code but works with any MCP client.

## What it does

- **UART probing** -- detect baud rates, capture serial output, interact with debug consoles
- **Power control** -- voltage/current management with safety tiers
- **GPIO control** -- toggle pins for bootloader entry (ESP32, ESP8266, etc.)
- **Flash extraction** -- dump firmware through UART bridge via esptool
- **Engagement logging** -- timestamped raw logs, per-engagement folders, config.json

## Requirements

- Python 3.11+
- BusPirate 6 with BPIO2 binary mode enabled
- User in `dialout` group for serial access (`sudo usermod -aG dialout $USER`)

## Install

```bash
git clone --recurse-submodules https://github.com/mplogas/buspirate-mcp.git
cd buspirate-mcp
pip install -e ".[dev]"
```

## MCP Client Configuration

Copy the example config and adjust paths for your machine:

```bash
cp .mcp.json.example .mcp.json
# Edit .mcp.json with the absolute path to your venv's python
```

The `.mcp.json` is gitignored since paths are machine-specific. Example config:

```json
{
  "mcpServers": {
    "buspirate": {
      "command": "/path/to/.venv/bin/python",
      "args": ["-m", "buspirate_mcp"]
    }
  }
}
```

Set `PIDEV_ENGAGEMENTS_DIR` environment variable to control where engagement logs are written. Defaults to `./engagements/` relative to the package root.

## Tools

| Tool | Safety Tier | Description |
|---|---|---|
| `list_devices` | read-only | Find BusPirate devices on USB |
| `verify_connection` | read-only | Check for signal activity on pins |
| `scan_baud` | read-only | Auto-detect baud rate |
| `read_output` | read-only | Read UART data from target |
| `open_uart` | allowed-write | Open persistent UART session with logging |
| `send_command` | allowed-write | Send command and capture response |
| `close_uart` | allowed-write | Close session, finalize logs |
| `enter_download_mode` | allowed-write | GPIO toggle for bootloader entry |
| `read_flash` | allowed-write | Dump flash via esptool through UART bridge |
| `set_voltage` | approval-write | Set PSU voltage (requires confirmation) |
| `set_power` | approval-write | Enable/disable PSU (requires confirmation) |

## Safety Model

Three tiers enforced at the MCP server boundary:

- **read-only** -- full autonomy, no side effects
- **allowed-write** -- autonomous execution, all calls logged
- **approval-write** -- blocks until human confirms via `_confirmed` parameter. Wrong voltage fries chips.

## BusPirate 6 Setup

### Enable BPIO2 binary mode

Connect to the BP6 terminal port (ACM0) and run:

```
binmode -> select 2 (BPIO2 flatbuffer interface) -> y to save
```

This persists across reboots.

### Pin mapping

UART mode uses IO4 (TX->) and IO5 (RX<-). The BP6 display is the authoritative source for pin assignment.

Free pins (IO0-IO3, IO6-IO7) can be used for GPIO control (bootloader entry, reset) while UART is active.

### Known limitations

- **Bridge mode exit:** After flash dump operations, bridge mode stays active. Press the physical BP6 button or USB-replug to exit.
- **Dual-port operation:** BPIO2 (binary port) and bridge mode (terminal port) cannot operate simultaneously.
- **Baud scan timing:** Fast-booting targets may finish output before the scan reaches the correct rate.
- **Port numbering:** The binary port number may change after USB replug (ACM1 -> ACM2). The server auto-detects.

## Tests

```bash
pytest              # 77 tests, no hardware needed
pytest -m hardware  # integration tests, BP6 must be connected
```

## License

MIT
