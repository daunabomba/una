"""
QEMU emulation commands for una.
"""

from pathlib import Path
from typing import Optional

QEMU_COMMANDS = {
    "x32": [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-no-reboot",
        "-m",
        "1G",
        "-machine",
        "q35",
        "-cpu",
        "host",
        "-drive",
        "if=pflash,format=raw,readonly=on,file=/etc/bios/OVMF.fd",
        "-serial",
        "mon:stdio",
        "-netdev",
        "user,id=vmnic,restrict=n,hostfwd=tcp::2022-:22",
        "-device",
        "virtio-net-pci,romfile=,netdev=vmnic",
        "-nodefaults",
        "-nographic",
    ],
    "x86_64": [
        "qemu-system-x86_64",
        "-enable-kvm",
        "-no-reboot",
        "-m",
        "1G",
        "-machine",
        "q35",
        "-cpu",
        "host",
        "-drive",
        "if=pflash,format=raw,readonly=on,file=/etc/bios/OVMF.fd",
        "-serial",
        "mon:stdio",
        "-netdev",
        "user,id=vmnic,restrict=n",
        "-device",
        "virtio-net-pci,romfile=,netdev=vmnic",
        "-nodefaults",
        "-nographic",
    ],
    "aarch64": [
        "qemu-system-aarch64",
        "-no-reboot",
        "-M",
        "virt",
        "-cpu",
        "cortex-a57",
        "-m",
        "1G",
        "-serial",
        "mon:stdio",
        "-netdev",
        "user,id=vmnic,restrict=n",
        "-device",
        "virtio-net-pci,romfile=,netdev=vmnic",
        "-nodefaults",
        "-nographic",
    ],
    "riscv64": [
        "qemu-system-riscv64",
        "-no-reboot",
        "-M",
        "virt",
        "-m",
        "1G",
        "-serial",
        "mon:stdio",
        "-netdev",
        "user,id=vmnic,restrict=n",
        "-device",
        "virtio-net-pci,romfile=,netdev=vmnic",
        "-nodefaults",
        "-nographic",
    ],
}


def get_qemu_command(
    arch: str, kernel_path: Path, append_args: str = "console=ttyS0"
) -> list:
    """
    Get QEMU command line for given architecture.

    Args:
        arch: Architecture (x32, x86_64, aarch64, riscv64)
        kernel_path: Path to kernel image
        append_args: Kernel command line arguments

    Returns:
        List of command arguments

    Raises:
        ValueError: If architecture not supported
    """
    if arch not in QEMU_COMMANDS:
        raise ValueError(f"Unsupported architecture: {arch}")

    base_cmd = QEMU_COMMANDS[arch].copy()
    base_cmd.extend(["-kernel", str(kernel_path), "-append", append_args])

    return base_cmd


def add_test_disk(cmd: list, disk_path: Path) -> list:
    """Add test disk to QEMU command."""
    if disk_path and disk_path.exists():
        return cmd + ["-drive", f"file={disk_path},format=raw,if=virtio"]
    return cmd


def get_console_args(arch: str) -> str:
    """Get kernel console argument for architecture."""
    console_map = {
        "x32": "console=ttyS0",
        "x86_64": "console=ttyS0",
        "aarch64": "console=ttyAMA0",
        "riscv64": "console=ttyS0",
    }
    return console_map.get(arch, "console=ttyS0")


def run_qemu(cmd: list) -> None:
    """Run QEMU with given command."""
    import subprocess

    print(f"Executing: {' '.join(cmd)}")
    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\nKernel execution stopped by user.")
    except Exception as e:
        print(f"Error during kernel execution: {e}")
        raise
