"""The flash agent has to be findable, or say why it is not.

OpenIPC/firmware#2381: a reporter with a bricked Hi3516CV300 was told to run
`defib agent flash` and got "No agent binary for 'hi3516cv300'" -- which reads
as an unsupported chip. hi3516cv300 is supported; the agent is bare-metal C
compiled per SoC and no prebuilt binary ships in the package. Worse, the only
path ever searched was the git checkout four levels above the module, which in
an installed package resolves inside site-packages and can never exist, so the
whole `defib agent` family was unreachable for anyone who installed defib the
documented way.
"""
from __future__ import annotations

from pathlib import Path

from defib.agent.client import (
    _CHIP_TO_AGENT,
    _agent_search_path,
    agent_binary_for,
    agent_binary_help,
    get_agent_binary,
)


class TestSearchPath:
    def test_an_installed_package_has_somewhere_to_look(self):
        """More than the checkout, which an installed defib does not have."""
        paths = _agent_search_path("hi3516cv300")
        assert len(paths) > 1

    def test_the_env_override_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEFIB_AGENT_DIR", str(tmp_path))
        assert _agent_search_path("hi3516cv300")[0] == tmp_path / "agent-hi3516cv300.bin"

    def test_a_binary_in_the_env_dir_is_found(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEFIB_AGENT_DIR", str(tmp_path))
        (tmp_path / "agent-hi3516cv300.bin").write_bytes(b"\x00" * 16)
        assert get_agent_binary("hi3516cv300") == tmp_path / "agent-hi3516cv300.bin"

    def test_a_variant_suffix_still_resolves(self, monkeypatch, tmp_path):
        """`hi3516av300:emmc` differs only in DDR init, not in the agent."""
        monkeypatch.setenv("DEFIB_AGENT_DIR", str(tmp_path))
        (tmp_path / "agent-hi3516cv500.bin").write_bytes(b"\x00" * 16)
        assert get_agent_binary("hi3516av300:emmc") is not None

    def test_every_path_is_absolute(self):
        assert all(Path(p).is_absolute() for p in _agent_search_path("hi3516cv300"))

    def test_a_build_named_after_the_chip_is_found_too(self, monkeypatch, tmp_path):
        """`make SOC=gk7205v300` writes agent-gk7205v300.bin, not the mapped name."""
        monkeypatch.setenv("DEFIB_AGENT_DIR", str(tmp_path))
        (tmp_path / "agent-gk7205v300.bin").write_bytes(b"\x00" * 16)
        assert get_agent_binary("gk7205v300") == tmp_path / "agent-gk7205v300.bin"

    def test_the_mapped_build_is_still_found_when_that_is_what_exists(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEFIB_AGENT_DIR", str(tmp_path))
        (tmp_path / "agent-gk7205v200.bin").write_bytes(b"\x00" * 16)
        assert get_agent_binary("gk7205v300") == tmp_path / "agent-gk7205v200.bin"

    def test_every_chip_the_agent_makefile_builds_is_reachable(self):
        """The map is what `defib agent` will accept; the Makefile is the truth."""
        makefile = (
            Path(__file__).parent.parent / "agent" / "Makefile"
        ).read_text()
        built = {
            line.split("$(SOC),")[1].split(")")[0]
            for line in makefile.splitlines()
            if "ifeq ($(SOC)," in line or "else ifeq ($(SOC)," in line
        }
        assert built, "could not read the SOC list out of agent/Makefile"
        assert built <= set(_CHIP_TO_AGENT), (
            f"agent/Makefile builds {sorted(built - set(_CHIP_TO_AGENT))}, "
            f"which defib agent will refuse"
        )


class TestTheMessage:
    def test_a_supported_chip_is_not_reported_as_unsupported(self, monkeypatch, tmp_path):
        """This is the sentence that misdirected the reporter."""
        monkeypatch.setenv("DEFIB_AGENT_DIR", str(tmp_path))
        msg = agent_binary_help("hi3516cv300")
        assert "no prebuilt binary" in msg
        assert "make SOC=hi3516cv300" in msg

    def test_it_names_the_shared_build_when_they_differ(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEFIB_AGENT_DIR", str(tmp_path))
        msg = agent_binary_help("hi3516dv300")
        assert "hi3516cv500 build" in msg
        assert "make SOC=hi3516cv500" in msg

    def test_it_says_where_it_looked(self, monkeypatch, tmp_path):
        monkeypatch.setenv("DEFIB_AGENT_DIR", str(tmp_path))
        assert str(tmp_path) in agent_binary_help("hi3516cv300")

    def test_it_offers_the_route_that_needs_no_compiler(self):
        """install/restore drive U-Boot over TFTP and want nothing built."""
        msg = agent_binary_help("hi3516cv300")
        assert "defib install" in msg and "defib restore" in msg

    def test_a_chip_with_no_agent_says_so_and_lists_what_has_one(self):
        msg = agent_binary_help("hi3516cv200")
        assert "no flash agent for 'hi3516cv200'" in msg
        assert "hi3516cv300" in msg
        assert "defib install" in msg

    def test_agent_binary_for_agrees_with_the_map(self):
        for chip, build in _CHIP_TO_AGENT.items():
            assert agent_binary_for(chip) == build
        assert agent_binary_for("nonesuch") is None
