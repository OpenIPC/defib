"""Tests for the CLI interface."""

import re
from types import SimpleNamespace

from typer.testing import CliRunner

from defib.cli.app import app

runner = CliRunner()


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape sequences from text."""
    return re.sub(r"\x1b\[[0-9;]*m", "", text)


class TestListChips:
    def test_list_chips_human(self):
        result = runner.invoke(app, ["list-chips"])
        assert result.exit_code == 0
        assert "hi3516cv300" in result.stdout
        assert "supported chips" in result.stdout.lower()

    def test_list_chips_json(self):
        import json
        result = runner.invoke(app, ["list-chips", "--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "chips" in data
        assert "count" in data
        assert data["count"] > 50
        assert "hi3516cv300" in data["chips"]


class TestBurnHelp:
    def test_burn_help(self):
        result = runner.invoke(app, ["burn", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.stdout)
        assert "--chip" in output
        assert "--file" in output
        assert "--port" in output


class TestPortsCommand:
    def test_ports_runs(self):
        result = runner.invoke(app, ["ports"])
        # May find ports or not, but should not crash
        assert result.exit_code == 0

    def test_ports_json(self):
        import json
        result = runner.invoke(app, ["ports", "--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert "ports" in data

    def test_ports_json_includes_rich_metadata(self, monkeypatch):
        import json

        fake_port = SimpleNamespace(
            device="/dev/ttyUSB1",
            description="FT232R USB UART - FT232R USB UART",
            hwid="USB VID:PID=0403:6001 SER=A50285BI LOCATION=5-2",
            alias_device="/dev/uart-orangepi5plus",
            open_path="/dev/uart-orangepi5plus",
            display_name="/dev/uart-orangepi5plus -> /dev/ttyUSB1 | FTDI FT232R USB UART | loc 5-2 | ser A50285BI",
            manufacturer="FTDI",
            product="FT232R USB UART",
            serial_number="A50285BI",
            location="5-2",
            vid=0x0403,
            pid=0x6001,
        )
        monkeypatch.setattr("defib.serial_ports.list_serial_ports", lambda: [fake_port])

        result = runner.invoke(app, ["ports", "--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["ports"][0]["device"] == "/dev/ttyUSB1"
        assert data["ports"][0]["alias_device"] == "/dev/uart-orangepi5plus"
        assert data["ports"][0]["display_name"].startswith("/dev/uart-orangepi5plus -> /dev/ttyUSB1")

    def test_ports_human_shows_alias_and_location(self, monkeypatch):
        fake_port = SimpleNamespace(
            device="/dev/ttyUSB1",
            description="FT232R USB UART - FT232R USB UART",
            hwid="USB VID:PID=0403:6001 SER=A50285BI LOCATION=5-2",
            alias_device="/dev/uart-orangepi5plus",
            open_path="/dev/uart-orangepi5plus",
            display_name="/dev/uart-orangepi5plus -> /dev/ttyUSB1 | FTDI FT232R USB UART | loc 5-2 | ser A50285BI",
            manufacturer="FTDI",
            product="FT232R USB UART",
            serial_number="A50285BI",
            location="5-2",
            vid=0x0403,
            pid=0x6001,
        )
        monkeypatch.setattr("defib.serial_ports.list_serial_ports", lambda: [fake_port])

        result = runner.invoke(app, ["ports"])
        assert result.exit_code == 0
        output = _strip_ansi(result.stdout)
        assert "/dev/uart-" in output
        assert "/dev/ttyUSB1" in output
        assert "5-2" in output


class TestDetectHelp:
    def test_detect_help(self):
        result = runner.invoke(app, ["detect", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.stdout)
        assert "--port" in output
        assert "--timeout" in output


class TestCaptureHelp:
    def test_capture_help(self):
        result = runner.invoke(app, ["capture", "--help"])
        assert result.exit_code == 0
        output = _strip_ansi(result.stdout)
        assert "--port" in output
        assert "--output" in output

    def test_capture_missing_args(self):
        result = runner.invoke(app, ["capture"])
        assert result.exit_code != 0


class TestReplayCommand:
    def test_replay_nonexistent_file(self):
        result = runner.invoke(app, ["replay", "/tmp/nonexistent_defib_test.dcap"])
        assert result.exit_code != 0

    def test_replay_valid_capture(self, tmp_path):
        from defib.capture.format import CaptureFile
        cap = CaptureFile(chip="test_chip", baudrate=115200)
        cap.add_tx(0, b"\xfe\x00\xff\x01")
        cap.add_rx(100, b"\xaa")
        path = tmp_path / "test.dcap"
        cap.save(path)

        result = runner.invoke(app, ["replay", str(path)])
        assert result.exit_code == 0
        assert "test_chip" in result.stdout
        assert "115200" in result.stdout

    def test_replay_json(self, tmp_path):
        import json
        from defib.capture.format import CaptureFile
        cap = CaptureFile(chip="json_test")
        cap.add_tx(0, b"\x01")
        path = tmp_path / "test.dcap"
        cap.save(path)

        result = runner.invoke(app, ["replay", str(path), "--output", "json"])
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data["chip"] == "json_test"
        assert data["records"] == 1
