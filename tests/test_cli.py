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


class TestPoePortOverride:
    """`--poe-port` lets the user supply an explicit MikroTik ether port,
    bypassing find_port_by_comment auto-discovery. Needed for /dev/ttyUSBN
    paths that have no /dev/uart-<label> symlink (and whose basename
    `ttyUSB2` won't prefix-match any interface comment).
    """

    def test_burn_help_documents_poe_port(self):
        result = runner.invoke(app, ["burn", "--help"])
        assert result.exit_code == 0
        out = _strip_ansi(result.stdout)
        assert "--poe-port" in out

    def test_install_help_documents_poe_port(self):
        result = runner.invoke(app, ["install", "--help"])
        assert result.exit_code == 0
        out = _strip_ansi(result.stdout)
        assert "--poe-port" in out

    def test_restore_help_documents_poe_port(self):
        result = runner.invoke(app, ["restore", "--help"])
        assert result.exit_code == 0
        out = _strip_ansi(result.stdout)
        assert "--poe-port" in out


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


class TestAgentNotRespondingDiagnostic:
    """The diagnostic shown when boot-protocol upload completes but no
    READY frame arrives — most often a board-variant DDR mismatch."""

    def test_mentions_ddr_init_root_cause(self):
        from defib.cli.app import _agent_not_responding_message
        msg = _agent_not_responding_message("hi3516av300", 0x81000000)
        assert "DDR" in msg
        # Surfaces the actual address so the user can match it against
        # what they see on the wire.
        assert "0x81000000" in msg

    def test_no_variants_path(self):
        from defib.cli.app import _agent_not_responding_message
        msg = _agent_not_responding_message("hi3516av300", 0x81000000)
        # Shipped profile has no variants today; message should say so
        # rather than pretending one exists.
        assert "No board variants declared" in msg

    def test_mentions_vendor_uboot_loadx_fallback(self):
        from defib.cli.app import _agent_not_responding_message
        msg = _agent_not_responding_message("hi3516av300", 0x81000000)
        assert "loady" in msg
        assert "go 0x81000000" in msg

    def test_when_variants_exist_lists_them(self, monkeypatch):
        # Pretend the chip ships with two variants
        import defib.cli.app as cli_app
        from defib.profiles import loader
        monkeypatch.setattr(
            loader, "list_variants", lambda *a, **kw: ["emmc", "nor"],
        )
        msg = cli_app._agent_not_responding_message("hi3516av300", 0x81000000)
        assert "emmc" in msg
        assert "nor" in msg
        # And a concrete next-command nudge
        assert "defib agent upload -c hi3516av300:" in msg
