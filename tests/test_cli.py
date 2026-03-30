"""Tests for the CLI interface."""

import re

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
