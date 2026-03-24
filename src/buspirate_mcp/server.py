"""BusPirate MCP server -- stdio transport.

Registers all tools from tools.py with the MCP SDK and runs the
server. Claude Code spawns this process and communicates over stdin/stdout.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from buspirate_mcp.hardware import BusPirateHardware
from buspirate_mcp.safety import classify_tool, SafetyTier
from buspirate_mcp.session import SessionManager
from buspirate_mcp import tools

logger = logging.getLogger("buspirate-mcp")

# Engagements dir: env var overrides, fallback to package root.
# In standalone mode: defaults to <repo>/engagements/
# When submoduled: parent repo sets PIDEV_ENGAGEMENTS_DIR via .mcp.json env.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
ENGAGEMENTS_DIR = Path(
    os.environ.get("PIDEV_ENGAGEMENTS_DIR", str(_PACKAGE_ROOT / "engagements"))
)

app = Server("buspirate-mcp")
session_manager = SessionManager(engagements_dir=ENGAGEMENTS_DIR)

# Hardware connection -- initialized lazily on first tool call that needs it
_hardware: BusPirateHardware | None = None
_hardware_port: str = ""


def _get_hardware(port: str | None = None) -> BusPirateHardware:
    global _hardware, _hardware_port
    if _hardware is None:
        if port is None:
            devices = BusPirateHardware.list_devices()
            if not devices:
                raise RuntimeError("No BusPirate found on USB")
            # Find the binary protocol port; fall back to first device
            binary = [d for d in devices if d["role"] == "binary"]
            port = binary[0]["path"] if binary else devices[0]["path"]
        _hardware = BusPirateHardware.connect(port)
        _hardware_port = port
    return _hardware


TOOL_DEFINITIONS = [
    Tool(
        name="list_devices",
        description="Find BusPirate devices on USB CDC serial ports. [read-only]",
        inputSchema={"type": "object", "properties": {}, "required": []},
    ),
    Tool(
        name="verify_connection",
        description=(
            "Check for signal activity on specified pins. "
            "User must provide pin mapping. [read-only]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "tx_pin": {"type": "integer", "description": "BP IO pin number for TX (e.g., 4)"},
                "rx_pin": {"type": "integer", "description": "BP IO pin number for RX (e.g., 5)"},
                "sample_duration_ms": {
                    "type": "integer", "default": 2000,
                    "description": "How long to sample for activity (ms)",
                },
            },
            "required": ["tx_pin", "rx_pin"],
        },
    ),
    Tool(
        name="scan_baud",
        description=(
            "Scan common baud rates and score by readable ASCII output. "
            "Returns candidates sorted by confidence. [read-only]"
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "required": [],
        },
    ),
    Tool(
        name="open_uart",
        description=(
            "Open a persistent UART connection and start an engagement. "
            "Creates engagement folder and raw logging. [allowed-write]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "baud": {"type": "integer", "description": "Baud rate"},
                "tx_pin": {"type": "integer", "description": "BP IO pin number for TX (e.g., 4)", "default": 4},
                "rx_pin": {"type": "integer", "description": "BP IO pin number for RX (e.g., 5)", "default": 5},
                "engagement_name": {"type": "string", "description": "Target device name"},
            },
            "required": ["baud", "engagement_name"],
        },
    ),
    Tool(
        name="read_output",
        description=(
            "Read available data from an open UART session. "
            "Blocks up to timeout. [read-only]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 5000},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="send_command",
        description=(
            "Send a command over UART and capture the response. [allowed-write]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "command": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 3000},
            },
            "required": ["session_id", "command"],
        },
    ),
    Tool(
        name="close_uart",
        description="Close a UART session and finalize logs. [allowed-write]",
        inputSchema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
            },
            "required": ["session_id"],
        },
    ),
    Tool(
        name="set_voltage",
        description=(
            "Set power supply voltage (0.8-5.0V) and current limit (0-500mA). "
            "DANGEROUS: wrong voltage can damage target hardware. [approval-write]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "voltage_v": {"type": "number", "minimum": 0.8, "maximum": 5.0},
                "current_limit_ma": {"type": "integer", "minimum": 0, "maximum": 500},
                "_confirmed": {"type": "boolean", "default": False, "description": "Set true to confirm execution"},
            },
            "required": ["voltage_v", "current_limit_ma"],
        },
        annotations={"destructiveHint": True, "readOnlyHint": False},
    ),
    Tool(
        name="set_power",
        description=(
            "Enable or disable the power supply. "
            "DANGEROUS: affects power to target device. [approval-write]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "enable": {"type": "boolean"},
                "_confirmed": {"type": "boolean", "default": False, "description": "Set true to confirm execution"},
            },
            "required": ["enable"],
        },
        annotations={"destructiveHint": True, "readOnlyHint": False},
    ),
    Tool(
        name="enter_download_mode",
        description=(
            "Put target into UART download mode by toggling GPIO pins. "
            "Holds boot pin LOW, pulses reset pin, releases. "
            "Works for ESP32, ESP8266, and similar chips. [allowed-write]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "boot_pin": {
                    "type": "integer",
                    "description": "BP IO pin connected to target boot select (e.g., GPIO0 on ESP32)",
                },
                "reset_pin": {
                    "type": "integer",
                    "description": "BP IO pin connected to target EN/RST",
                },
            },
            "required": ["boot_pin", "reset_pin"],
        },
    ),
    Tool(
        name="read_flash",
        description=(
            "Read flash memory from target via esptool through BP6 UART bridge. "
            "Optionally enters download mode first. Requires esptool installed. [allowed-write]"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "offset": {
                    "type": "integer", "default": 0,
                    "description": "Flash offset to start reading from",
                },
                "size": {
                    "type": "integer",
                    "description": "Number of bytes to read (e.g., 0x400000 for 4MB)",
                },
                "output_path": {
                    "type": "string",
                    "description": "Path to save the flash dump",
                },
                "boot_pin": {
                    "type": "integer", "default": -1,
                    "description": "BP IO pin for boot select (-1 to skip download mode entry)",
                },
                "reset_pin": {
                    "type": "integer", "default": -1,
                    "description": "BP IO pin for reset (-1 to skip download mode entry)",
                },
                "baud": {
                    "type": "integer", "default": 115200,
                    "description": "Baud rate for flash read (115200 or 460800 recommended)",
                },
            },
            "required": ["size", "output_path"],
        },
    ),
]


@app.list_tools()
async def list_tools():
    return TOOL_DEFINITIONS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    tier = classify_tool(name)
    logger.info("tool=%s tier=%s args=%s", name, tier.value, arguments)

    # Approval-write tools require explicit confirmation token.
    # MCP clients that respect destructiveHint will prompt the user,
    # but we enforce server-side too for clients that don't.
    if tier == SafetyTier.APPROVAL_WRITE:
        if not arguments.get("_confirmed", False):
            desc = f"{name}({', '.join(f'{k}={v}' for k, v in arguments.items())})"
            return [TextContent(
                type="text",
                text=json.dumps({
                    "confirmation_required": True,
                    "tool": name,
                    "arguments": arguments,
                    "message": f"APPROVAL REQUIRED: {desc}. "
                    f"Re-call with _confirmed=true to execute.",
                }),
            )]
        # Remove the confirmation token before passing to the tool
        arguments = {k: v for k, v in arguments.items() if k != "_confirmed"}

    try:
        if name == "list_devices":
            result = await tools.tool_list_devices()

        elif name == "verify_connection":
            hw = _get_hardware()
            result = await tools.tool_verify_connection(
                hardware=hw,
                pins={"tx": arguments["tx_pin"], "rx": arguments["rx_pin"]},
                sample_duration_ms=arguments.get("sample_duration_ms", 2000),
            )

        elif name == "scan_baud":
            hw = _get_hardware()
            result = await tools.tool_scan_baud(hardware=hw)

        elif name == "open_uart":
            hw = _get_hardware()
            result = await tools.tool_open_uart(
                session_manager=session_manager,
                hardware=hw,
                baud=arguments["baud"],
                pins={
                    "tx": arguments.get("tx_pin", 4),
                    "rx": arguments.get("rx_pin", 5),
                },
                engagement_name=arguments["engagement_name"],
                device_path=_hardware_port,
            )

        elif name == "read_output":
            result = await tools.tool_read_output(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                timeout_ms=arguments.get("timeout_ms", 5000),
            )

        elif name == "send_command":
            result = await tools.tool_send_command(
                session_manager=session_manager,
                session_id=arguments["session_id"],
                command=arguments["command"],
                timeout_ms=arguments.get("timeout_ms", 3000),
            )

        elif name == "close_uart":
            result = await tools.tool_close_uart(
                session_manager=session_manager,
                session_id=arguments["session_id"],
            )

        elif name == "set_voltage":
            hw = _get_hardware()
            result = await tools.tool_set_voltage(
                hardware=hw,
                voltage_v=arguments["voltage_v"],
                current_limit_ma=arguments["current_limit_ma"],
            )

        elif name == "set_power":
            hw = _get_hardware()
            result = await tools.tool_set_power(
                hardware=hw,
                enable=arguments["enable"],
            )

        elif name == "enter_download_mode":
            hw = _get_hardware()
            result = await tools.tool_enter_download_mode(
                hardware=hw,
                boot_pin=arguments["boot_pin"],
                reset_pin=arguments["reset_pin"],
            )

        elif name == "read_flash":
            hw = _get_hardware()
            terminal_port = BusPirateHardware.find_terminal_port()
            if not terminal_port:
                result = {"error": "Cannot find BP6 terminal port for bridge mode"}
            else:
                result = await tools.tool_read_flash(
                    hardware=hw,
                    terminal_port=terminal_port,
                    offset=arguments.get("offset", 0),
                    size=arguments["size"],
                    output_path=arguments["output_path"],
                    boot_pin=arguments.get("boot_pin", -1),
                    reset_pin=arguments.get("reset_pin", -1),
                    baud=arguments.get("baud", 115200),
                )

        else:
            result = {"error": f"Unknown tool: {name}"}

    except Exception as exc:
        logger.error("tool=%s error=%s", name, exc)
        result = {"error": str(exc), "tool": name}

    return [TextContent(type="text", text=json.dumps(result, indent=2))]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
