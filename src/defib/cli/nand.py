"""``defib agent nand`` — SPI NAND study commands over the flash agent.

Low-level access to the FMC100 SPI NAND path for chasing ECC faults below
the OS: identify the chip and its feature registers, read pages raw or
through the controller's ECC engine, survey ECC status over a range, dump
page + OOB, and (destructively) program or erase single pages/blocks.

``write-page`` and ``erase-block`` change the flash immediately, with no
prompt, like every other writing command in defib.  Take a ``dump``
first.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, TypeVar

import typer
from rich.console import Console

nand_app = typer.Typer(help="SPI NAND study ops: raw pages, OOB, ECC status (FMC100 SoCs)")

T = TypeVar("T")

PORT_HELP = "Serial device (/dev/ttyUSB0), tcp://host:port, rfc2217://host:port, or socket:///path"
ECC_CHOICES = {"none": 0, "8": 1, "16": 2, "24": 3, "28": 4, "40": 5, "64": 6}


def _run(port: str, body: Callable[[Any], Awaitable[T]]) -> T:
    """Connect to the agent on ``port``, run ``body(NandStudy)``, close."""
    from defib.agent.client import FlashAgentClient
    from defib.agent.nand import NandError, NandStudy
    from defib.transport.serial_platform import create_transport, normalize_port_name

    console = Console(stderr=True)

    async def go() -> T:
        transport = await create_transport(normalize_port_name(port))
        try:
            client = FlashAgentClient(transport)
            if not await client.connect(timeout=5.0):
                console.print("[red]Agent not responding.[/red] Upload it first with "
                              "'defib agent upload'.")
                raise typer.Exit(1)
            info = await client.get_info()
            if not int(info.get("capabilities", 0)) & FlashAgentClient.CAP_NAND:
                console.print("[red]This agent has no CMD_NAND[/red] (agent version "
                              f"{info.get('agent_version', '?')}); rebuild and re-upload it.")
                raise typer.Exit(1)
            return await body(NandStudy(client))
        except NandError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        finally:
            await transport.close()

    return asyncio.run(go())


def _xfer(
    study: Any, mode: str, ecc: str, fmc_cfg: str, opcode: str, iftype: int, dummy: int,
) -> Any:
    """Build a NandXfer; the FMC_CFG base is the live register."""
    from defib.agent.nand import EccType, NandXfer, XferMode, compose_fmc_cfg

    if mode not in ("reg", "dma"):
        raise typer.BadParameter("--mode must be reg or dma")
    if fmc_cfg:
        cfg = int(fmc_cfg, 0)
    elif mode == "reg":
        cfg = 0
    else:
        if ecc not in ECC_CHOICES:
            raise typer.BadParameter(f"--ecc must be one of {', '.join(ECC_CHOICES)}")
        assert study._info is not None
        cfg = compose_fmc_cfg(study._info.fmc_cfg, EccType(ECC_CHOICES[ecc]))
    return NandXfer(
        mode=XferMode.REG if mode == "reg" else XferMode.DMA,
        fmc_cfg=cfg, opcode=int(opcode, 0), iftype=iftype, dummy=dummy,
    )


def _describe(rec: Any, steps: int) -> str:
    parts = []
    errs = rec.step_errors(steps)
    if any(errs):
        parts.append("ecc " + "/".join("UNCORR" if n == 0xFF else str(n) for n in errs))
    else:
        parts.append("ecc clean")
    parts.append(f"on-die {['clean', 'corrected', 'FAILED', 'corrected(3)'][rec.ondie_ecc]}")
    parts.append(f"status 0x{rec.ondie:02x}")
    if rec.failed:
        parts.append("[red]OP FAILED[/red]")
    return ", ".join(parts)


def _features(study: Any) -> Awaitable[dict[str, int]]:
    async def read() -> dict[str, int]:
        from defib.agent.nand import FEATURE_CONFIG, FEATURE_PROTECT, FEATURE_STATUS
        return {
            "protect_a0": await study.feature_get(FEATURE_PROTECT),
            "config_b0": await study.feature_get(FEATURE_CONFIG),
            "status_c0": await study.feature_get(FEATURE_STATUS),
        }
    return read()


# Shared options --------------------------------------------------------------

PortOpt = typer.Option("/dev/ttyUSB0", "-p", "--port", help=PORT_HELP)
ModeOpt = typer.Option("dma", "--mode", help="reg: raw register reads; dma: controller page engine")
EccOpt = typer.Option("8", "--ecc", help="Controller ECC for --mode dma: none, 8, 16, 24, 28, 40, 64")
CfgOpt = typer.Option("", "--fmc-cfg", help="Exact FMC_CFG value (hex); overrides --ecc")
OpcodeOpt = typer.Option("0x03", "--opcode", help="DMA read opcode: 0x03 std, 0x0B fast, 0x6B quad")
IftypeOpt = typer.Option(0, "--iftype", help="FMC interface: 0 std, 1 dual, 2 dio, 3 quad, 4 qio")
DummyOpt = typer.Option(1, "--dummy", help="Dummy bytes after the column address")


@nand_app.command("info")
def nand_info(port: str = PortOpt) -> None:
    """Chip geometry, feature registers and live FMC_CFG."""
    async def body(study: Any) -> None:
        info = await study.info()
        feats = await _features(study)
        print(f"Page {info.page_size} + OOB {info.oob_size}, "
              f"{info.pages_per_block} pages/block, {info.blocks} blocks "
              f"({info.pages * info.page_size // (1024 * 1024)} MiB)")
        print(f"FMC_CFG 0x{info.fmc_cfg:08x}")
        b0 = feats["config_b0"]
        print(f"Feature 0xA0 (protect) 0x{feats['protect_a0']:02x}")
        print(f"Feature 0xB0 (config)  0x{b0:02x}  on-die ECC {'ON' if b0 & 0x10 else 'off'}"
              f", BUF {1 if b0 & 0x08 else 0}, QE {b0 & 0x01}")
        print(f"Feature 0xC0 (status)  0x{feats['status_c0']:02x}")
        print(f"Agent buffers: pages @ 0x{info.dma_buf:08x} ({info.dma_size} B), "
              f"records @ 0x{info.stat_buf:08x} ({info.stat_size} B)")
    _run(port, body)


@nand_app.command("feature")
def nand_feature(
    addr: str = typer.Argument(..., help="Feature register, e.g. 0xB0"),
    value: str = typer.Argument("", help="Value to write (hex); omit to read"),
    port: str = PortOpt,
) -> None:
    """Read or write a SPI NAND feature register (volatile)."""
    async def body(study: Any) -> None:
        reg = int(addr, 0)
        if value:
            got = await study.feature_set(reg, int(value, 0))
            print(f"0x{reg:02x} <- 0x{int(value, 0):02x}, reads back 0x{got:02x}")
        else:
            print(f"0x{reg:02x} = 0x{await study.feature_get(reg):02x}")
    _run(port, body)


@nand_app.command("fmc-reg")
def nand_fmc_reg(
    offset: str = typer.Argument(..., help="FMC register offset, e.g. 0x00 (FMC_CFG)"),
    value: str = typer.Argument("", help="Value to write (hex); omit to read"),
    port: str = PortOpt,
) -> None:
    """Read or write a 32-bit FMC controller register."""
    async def body(study: Any) -> None:
        got = await study.fmc_reg(int(offset, 0), int(value, 0) if value else None)
        print(f"FMC+0x{int(offset, 0):03x} = 0x{got:08x}")
    _run(port, body)


@nand_app.command("read-page")
def nand_read_page(
    page: int = typer.Argument(..., help="Page number"),
    port: str = PortOpt,
    mode: str = ModeOpt,
    ecc: str = EccOpt,
    fmc_cfg: str = CfgOpt,
    opcode: str = OpcodeOpt,
    iftype: int = IftypeOpt,
    dummy: int = DummyOpt,
    output: str = typer.Option("", "-o", "--output", help="Write page + OOB to this file"),
) -> None:
    """Read one page + OOB and report what the controller and chip said."""
    async def body(study: Any) -> None:
        info = await study.info()
        x = _xfer(study, mode, ecc, fmc_cfg, opcode, iftype, dummy)
        r = await study.read_page(page, x)
        print(f"page {page} ({mode}, FMC_CFG 0x{x.fmc_cfg or info.fmc_cfg:08x}): "
              f"{_describe(r.record, info.ecc_steps)}")
        print(f"data[0:32] {r.data[:32].hex()}")
        for off in range(0, len(r.oob), 32):
            print(f"oob[{off:3d}]   {r.oob[off:off + 32].hex()}")
        if output:
            Path(output).write_bytes(r.data + r.oob)
            print(f"wrote {output}")
    _run(port, body)


@nand_app.command("ecc-scan")
def nand_ecc_scan(
    port: str = PortOpt,
    start: int = typer.Option(0, "--start", help="First page"),
    count: int = typer.Option(0, "--count", help="Pages to scan (0 = to the end)"),
    mode: str = ModeOpt,
    ecc: str = EccOpt,
    fmc_cfg: str = CfgOpt,
    opcode: str = OpcodeOpt,
    iftype: int = IftypeOpt,
    dummy: int = DummyOpt,
    json_out: str = typer.Option("", "--json", help="Write every page's record to this file"),
) -> None:
    """Read a page range and report ECC status per page (data is discarded)."""
    async def body(study: Any) -> None:
        info = await study.info()
        n = count or info.pages - start
        x = _xfer(study, mode, ecc, fmc_cfg, opcode, iftype, dummy)
        result = await study.scan(start, n, x)
        steps = info.ecc_steps
        uncorr = [(start + i, r) for i, r in enumerate(result.records) if r.uncorrectable_steps(steps)]
        corrected = [(start + i, r) for i, r in enumerate(result.records)
                     if not r.uncorrectable_steps(steps) and r.corrected_bits(steps)]
        print(f"scanned {len(result.records)}/{n} pages from {start} "
              f"(FMC_CFG 0x{x.fmc_cfg or info.fmc_cfg:08x}, {mode})")
        print(f"uncorrectable: {len(uncorr)}   corrected: {len(corrected)}")
        for page, rec in uncorr[:200]:
            blk, pg = divmod(page, info.pages_per_block)
            print(f"  page {page:6d} (block {blk:4d} page {pg:2d}) "
                  f"steps {rec.uncorrectable_steps(steps)} ecc_err 0x{rec.ecc_err:08x}")
        if len(uncorr) > 200:
            print(f"  ... {len(uncorr) - 200} more (see --json)")
        if json_out:
            Path(json_out).write_text(json.dumps({
                "start": start, "fmc_cfg": x.fmc_cfg or info.fmc_cfg, "mode": mode,
                "ecc_steps": steps, "pages_per_block": info.pages_per_block,
                "records": [[r.ecc_err, r.ondie, r.fmc_int, r.flags] for r in result.records],
            }))
            print(f"wrote {json_out}")
        if len(result.records) < n:
            print(f"[stopped early at page {start + len(result.records)}: controller timeout]",
                  file=sys.stderr)
    _run(port, body)


@nand_app.command("dump")
def nand_dump(
    output: str = typer.Option(..., "-o", "--output", help="Raw dump file (page + OOB per page)"),
    port: str = PortOpt,
    start: int = typer.Option(0, "--start", help="First page"),
    count: int = typer.Option(0, "--count", help="Pages (0 = to the end)"),
    mode: str = typer.Option("reg", "--mode", help="reg (default, raw array) or dma"),
    ecc: str = typer.Option("none", "--ecc", help="Controller ECC for --mode dma"),
    fmc_cfg: str = CfgOpt,
    keep_ondie: bool = typer.Option(
        False, "--keep-ondie-ecc",
        help="Leave the chip's on-die ECC as it is; by default it is switched off for the "
             "dump (raw bits) and restored afterwards",
    ),
) -> None:
    """Back up page + OOB, raw by default, with a JSON sidecar of per-page records."""
    async def body(study: Any) -> None:
        from defib.agent.nand import FEATURE_CONFIG

        info = await study.info()
        n = count or info.pages - start
        x = _xfer(study, mode, ecc, fmc_cfg, "0x03", 0, 1)
        feats = await _features(study)
        b0 = feats["config_b0"]
        toggled = not keep_ondie and bool(b0 & 0x10)
        batch = max(1, info.dma_size // info.stride)
        records: list[Any] = []
        failed_page = None
        try:
            if toggled:
                got = await study.feature_set(FEATURE_CONFIG, b0 & ~0x10)
                if got & 0x10:
                    Console(stderr=True).print(
                        f"[red]The chip kept its on-die ECC on (0xB0 reads 0x{got:02x}); "
                        "a dump now would not be raw.[/red] Use --keep-ondie-ecc to dump "
                        "corrected data anyway.")
                    raise typer.Exit(1)
            with open(output, "wb") as f:
                page = start
                while page < start + n:
                    k = min(batch, start + n - page)
                    recs, data = await study.read_pages(page, k, x)
                    # The agent stops after a failed page and still returns it;
                    # its bytes are whatever the buffer held, so keep them out.
                    good = len(recs) - 1 if recs and recs[-1].failed else len(recs)
                    f.write(data[:good * info.stride])
                    records.extend(recs[:good])
                    page += good
                    print(f"\r{page - start}/{n} pages", end="", file=sys.stderr, flush=True)
                    if good < k:
                        failed_page = page
                        break
        finally:
            if toggled:
                await study.feature_set(FEATURE_CONFIG, b0)
        print(file=sys.stderr)
        sidecar = Path(output + ".json")
        sidecar.write_text(json.dumps({
            "start": start, "pages": len(records), "requested": n,
            "complete": failed_page is None, "failed_page": failed_page,
            "page_size": info.page_size,
            "oob_size": info.oob_size, "pages_per_block": info.pages_per_block,
            "mode": mode, "fmc_cfg": x.fmc_cfg or info.fmc_cfg,
            "features": feats, "ondie_ecc_during_dump": bool(b0 & 0x10) and not toggled,
            "records": [[r.ecc_err, r.ondie, r.fmc_int, r.flags] for r in records],
        }))
        print(f"{len(records)} pages x {info.stride} B -> {output} (+ {sidecar.name})")
        if failed_page is not None:
            Console(stderr=True).print(
                f"[red]INCOMPLETE: page {failed_page} could not be read; "
                f"{n - len(records)} of {n} pages are missing.[/red]")
            raise typer.Exit(1)
    _run(port, body)


@nand_app.command("write-page")
def nand_write_page(
    page: int = typer.Argument(..., help="Page number"),
    input_file: str = typer.Option(..., "-i", "--input", help="Page data, or page + OOB"),
    port: str = PortOpt,
    mode: str = ModeOpt,
    ecc: str = EccOpt,
    fmc_cfg: str = CfgOpt,
    kernel_oob: bool = typer.Option(
        False, "--kernel-oob",
        help="Input is page data only; add the OOB a Linux hifmc100 write sends "
             "(0xff with the empty-page mark zeroed)",
    ),
    quad: bool = typer.Option(False, "--quad", help="Program with 0x32 over 4 lines"),
) -> None:
    """DESTRUCTIVE: program one page (+ OOB) exactly as given. No prompt."""
    async def body(study: Any) -> None:
        from defib.agent.nand import kernel_oob as make_oob
        from defib.agent.nand import program_xfer

        info = await study.info()
        payload = Path(input_file).read_bytes()
        if kernel_oob:
            if len(payload) != info.page_size:
                raise typer.BadParameter(f"--kernel-oob needs exactly {info.page_size} bytes")
            payload += make_oob(info.oob_size)
        if len(payload) != info.stride:
            raise typer.BadParameter(f"need {info.stride} bytes (page + OOB), got {len(payload)}")
        read_x = _xfer(study, mode, ecc, fmc_cfg, "0x6B" if quad else "0x03", 3 if quad else 0, 1)
        rec = await study.program_page(page, payload, program_xfer(read_x))
        print(f"programmed page {page}: status 0x{rec.ondie:02x}"
              f"{' FAILED' if rec.failed else ''}")
        if rec.failed:
            raise typer.Exit(1)
    _run(port, body)


@nand_app.command("erase-block")
def nand_erase_block(
    page: int = typer.Argument(..., help="Any page in the block"),
    port: str = PortOpt,
) -> None:
    """DESTRUCTIVE: erase the 128 KiB block containing PAGE. No prompt."""
    async def body(study: Any) -> None:
        info = await study.info()
        rec = await study.erase_block(page)
        print(f"erased block {page // info.pages_per_block}: status 0x{rec.ondie:02x}"
              f"{' FAILED' if rec.failed else ''}")
        if rec.failed:
            raise typer.Exit(1)
    _run(port, body)
