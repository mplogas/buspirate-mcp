"""Session management and logging for UART and bus protocols (SPI, I2C, 1-Wire)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_ANSI_ESCAPE = re.compile(
    r"\x1b"           # ESC byte
    r"(?:"
    r"\[[0-9;]*"      # CSI sequence (possibly incomplete at chunk boundary)
    r"[a-zA-Z]?"      # optional final byte (may be in next chunk)
    r")"
)


def _clean_text(text: str) -> str:
    """Remove ANSI/VT100 escape sequences, stray ESC bytes, and null bytes."""
    text = _ANSI_ESCAPE.sub("", text)
    text = text.replace("\x1b", "")   # catch any remaining lone ESC bytes
    text = text.replace("\x00", "")
    return text


def _sanitize_name(name: str) -> str:
    """Strip everything except alphanumeric, hyphens, underscores."""
    return re.sub(r"[^a-zA-Z0-9_-]", "", name)


class Session:
    """A single active UART session with logging."""

    def __init__(
        self,
        session_id: str,
        engagement_path: Path,
        hardware: Any,
        baud: int,
        pins: dict[str, str],
    ) -> None:
        self.session_id = session_id
        self.engagement_path = engagement_path
        self.hardware = hardware
        self.baud = baud
        self.pins = pins
        self.connected = True

        log_path = engagement_path / "logs" / "uart-raw.log"
        self._log_file = open(log_path, "a", encoding="utf-8")

    def log_rx(self, data: bytes) -> None:
        """Log received data with timestamp. Strips ANSI escape codes."""
        if not self.connected:
            raise ValueError("Session is disconnected")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = _clean_text(data.decode("utf-8", errors="replace"))
        self._log_file.write(f"[{ts}] RX: {text}\n")
        self._log_file.flush()

    def log_tx(self, data: bytes) -> None:
        """Log transmitted data with timestamp. Strips ANSI escape codes."""
        if not self.connected:
            raise ValueError("Session is disconnected")
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        text = _clean_text(data.decode("utf-8", errors="replace"))
        self._log_file.write(f"[{ts}] TX: {text}\n")
        self._log_file.flush()

    def close(self) -> None:
        """Close the log file and mark session as disconnected. Safe to call twice.

        Does NOT disconnect the hardware -- the hardware connection is
        shared across sessions and managed by the server. Only the log
        file is closed here.
        """
        if not self.connected:
            return
        self.connected = False
        self._log_file.close()
        self._log_file = None


class TransactionSession:
    """Session for discrete bus transactions (SPI, I2C, 1-Wire). Logs to JSONL."""

    def __init__(
        self,
        session_id: str,
        engagement_path: Path,
        hardware: Any,
        protocol: str,
    ) -> None:
        self.session_id = session_id
        self.engagement_path = Path(engagement_path)
        self.hardware = hardware
        self.protocol = protocol
        self.connected = True
        log_name = f"{protocol}-commands.jsonl"
        self._log_file = (self.engagement_path / "logs" / log_name).open(
            "a", encoding="utf-8"
        )

    def log_transaction(
        self,
        operation: str,
        write_hex: str = "",
        read_hex: str = "",
        metadata: dict | None = None,
    ) -> None:
        """Log a bus transaction as a JSONL line."""
        if not self.connected:
            raise ValueError("Session is disconnected")
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "operation": operation,
            "tx": write_hex,
            "rx": read_hex,
        }
        if metadata:
            entry["meta"] = metadata
        self._log_file.write(json.dumps(entry) + "\n")
        self._log_file.flush()

    def close(self) -> None:
        """Close the log file and mark session as disconnected. Safe to call twice."""
        if not self.connected:
            return
        self.connected = False
        self._log_file.close()
        self._log_file = None


# Protocol prefix for standalone engagement folder names.
_PROTOCOL_FOLDER_PREFIX = {
    "uart": "BP",
    "spi": "SPI",
    "i2c": "I2C",
    "1wire": "1W",
    "la": "LA",
}

# Subfolder name when nested under a project path.
_PROTOCOL_PROJECT_SUBDIR = {
    "uart": "uart",
    "spi": "spi",
    "i2c": "i2c",
    "1wire": "onewire",
    "la": "la",
}


class SessionManager:
    """Manages active UART and bus protocol sessions."""

    def __init__(self, engagements_dir: Path | str) -> None:
        self._engagements_dir = Path(engagements_dir)
        self._sessions: dict[str, Session | TransactionSession] = {}

    def create(
        self,
        name: str,
        hardware: Any,
        baud: int = 0,
        pins: dict[str, str] | None = None,
        device_path: str = "",
        project_path: str | None = None,
        protocol: str = "uart",
        protocol_config: dict | None = None,
    ) -> Session | TransactionSession:
        """Create a new engagement session with logging directory."""
        prefix = _PROTOCOL_FOLDER_PREFIX.get(protocol)
        if prefix is None:
            raise ValueError(f"Unknown protocol: {protocol}")

        if project_path is not None:
            resolved = Path(project_path).resolve()
            if not resolved.is_relative_to(self._engagements_dir.resolve()):
                raise ValueError("project_path must be under engagements directory")
            subdir = _PROTOCOL_PROJECT_SUBDIR[protocol]
            engagement_path = resolved / subdir
            engagement_path.mkdir(parents=True, exist_ok=True)
            (engagement_path / "logs").mkdir(exist_ok=True)
            (engagement_path / "artifacts").mkdir(exist_ok=True)
        else:
            sanitized = _sanitize_name(name)
            if not sanitized:
                sanitized = "unnamed"
            timestamp = datetime.now().strftime("%d-%m-%Y-%H-%M")

            folder_name = f"{timestamp}_{prefix}_{sanitized}"
            engagement_path = self._engagements_dir / folder_name
            counter = 1
            while engagement_path.exists():
                folder_name = f"{timestamp}_{prefix}_{sanitized}-{counter}"
                engagement_path = self._engagements_dir / folder_name
                counter += 1
            (engagement_path / "logs").mkdir(parents=True, exist_ok=True)
            (engagement_path / "artifacts").mkdir(parents=True, exist_ok=True)

        session_id = str(uuid.uuid4())[:8]
        now_ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

        # Write engagement config
        config: dict[str, Any] = {
            "session_id": session_id,
            "name": _sanitize_name(name),
            "device_path": device_path,
            "protocol": protocol,
            "created_at": now_ts,
        }

        if protocol == "uart":
            config["baud"] = baud
            config["pins"] = pins or {}
        else:
            config["protocol_config"] = protocol_config or {}

        config_path = engagement_path / "config.json"
        config_path.write_text(json.dumps(config, indent=2) + "\n")

        if protocol == "uart":
            session: Session | TransactionSession = Session(
                session_id=session_id,
                engagement_path=engagement_path,
                hardware=hardware,
                baud=baud,
                pins=pins or {},
            )
        else:
            session = TransactionSession(
                session_id=session_id,
                engagement_path=engagement_path,
                hardware=hardware,
                protocol=protocol,
            )

        self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> Session | TransactionSession:
        """Get an active session by ID. Raises KeyError if not found."""
        return self._sessions[session_id]

    def close(self, session_id: str) -> None:
        """Close and remove a session."""
        session = self._sessions.pop(session_id)
        session.close()
