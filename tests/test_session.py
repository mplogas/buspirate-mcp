"""Tests for UART session lifecycle and raw logging."""

import json
import re
import pytest
from buspirate_mcp.session import SessionManager, Session, _clean_text


class TestCleanText:
    def test_strips_full_ansi_sequence(self):
        assert _clean_text("\x1b[0;32mHello\x1b[0m") == "Hello"

    def test_strips_partial_ansi_at_chunk_boundary(self):
        # Partial CSI sequences (missing final letter) are also stripped
        assert _clean_text("data\x1b[0;3") == "data"

    def test_strips_lone_esc(self):
        assert _clean_text("before\x1bafter") == "beforeafter"

    def test_strips_null_bytes(self):
        assert _clean_text("hello\x00world") == "helloworld"

    def test_combined_garbage(self):
        text = "\x1b[0;32m[INFO]\x1b[0m data\x00here\x1b"
        result = _clean_text(text)
        assert "\x1b" not in result
        assert "\x00" not in result
        assert "INFO" in result

    def test_idempotent(self):
        text = "\x1b[0;32mtest\x1b[0m"
        result = _clean_text(text)
        assert _clean_text(result) == result


class TestSessionManager:
    def test_create_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="test-device",
            hardware=None,
            baud=115200,
            pins={"tx": 4, "rx": 5},
        )
        assert session.session_id is not None
        assert session.baud == 115200

    def test_engagement_folder_created(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="test-device",
            hardware=None,
            baud=115200,
            pins={"tx": 4, "rx": 5},
        )
        assert session.engagement_path.exists()
        assert (session.engagement_path / "logs").is_dir()
        assert (session.engagement_path / "artifacts").is_dir()

    def test_config_json_written(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="config-test",
            hardware=None,
            baud=115200,
            pins={"tx": 4, "rx": 5},
            device_path="/dev/ttyACM1",
        )
        config_path = session.engagement_path / "config.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text())
        assert config["baud"] == 115200
        assert config["pins"] == {"tx": 4, "rx": 5}
        assert config["device_path"] == "/dev/ttyACM1"
        assert config["name"] == "config-test"
        assert "created_at" in config

    def test_engagement_name_sanitized(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="../../etc/evil device!",
            hardware=None,
            baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        assert ".." not in session.engagement_path.name
        assert "/" not in session.engagement_path.name
        assert "!" not in session.engagement_path.name

    def test_engagement_name_date_prefixed(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="sensor-v3",
            hardware=None,
            baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        assert re.match(r"\d{2}-\d{2}-\d{4}-\d{2}-\d{2}_BP_", session.engagement_path.name)

    def test_get_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        retrieved = mgr.get(session.session_id)
        assert retrieved is session

    def test_get_nonexistent_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        with pytest.raises(KeyError):
            mgr.get("nonexistent-id")

    def test_close_session(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        sid = session.session_id
        mgr.close(sid)
        with pytest.raises(KeyError):
            mgr.get(sid)

    def test_duplicate_names_get_unique_folders(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        s1 = mgr.create(
            name="device", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        s2 = mgr.create(
            name="device", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        assert s1.engagement_path != s2.engagement_path
        assert s1.session_id != s2.session_id

    def test_double_close_is_safe(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        session.close()
        session.close()

    def test_accepts_string_path(self, tmp_path):
        mgr = SessionManager(engagements_dir=str(tmp_path))
        session = mgr.create(
            name="test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        assert session.engagement_path.exists()


class TestSessionLogging:
    def test_log_rx_writes_to_file(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="log-test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        session.log_rx(b"Hello from target\n")
        session.log_rx(b"Second line\n")
        mgr.close(session.session_id)

        log_path = session.engagement_path / "logs" / "uart-raw.log"
        content = log_path.read_text()
        assert "Hello from target" in content
        assert "Second line" in content

    def test_log_rx_after_disconnect_raises(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="dc-test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        session.close()
        with pytest.raises(ValueError):
            session.log_rx(b"ghost data")

    def test_log_entries_are_timestamped(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="ts-test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        session.log_rx(b"data")
        mgr.close(session.session_id)

        log_path = session.engagement_path / "logs" / "uart-raw.log"
        content = log_path.read_text()
        assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", content)

    def test_log_tx_marked_as_sent(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="tx-test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        session.log_tx(b"uname -a\r\n")
        mgr.close(session.session_id)

        log_path = session.engagement_path / "logs" / "uart-raw.log"
        content = log_path.read_text()
        assert "TX" in content
        assert "uname -a" in content

    def test_log_strips_ansi(self, tmp_path):
        mgr = SessionManager(engagements_dir=tmp_path)
        session = mgr.create(
            name="ansi-test", hardware=None, baud=9600,
            pins={"tx": 4, "rx": 5},
        )
        session.log_rx(b"\x1b[0;32m[INFO] test\x1b[0m")
        mgr.close(session.session_id)

        log_path = session.engagement_path / "logs" / "uart-raw.log"
        content = log_path.read_text()
        assert "\x1b" not in content
        assert "INFO" in content
