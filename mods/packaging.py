"""
Packaging and deployment operations for una.
Creates test disks, runs kernels via QEMU, etc.
"""

import subprocess
import sys
from pathlib import Path

from mods import colors
from mods.emulation import get_qemu_command, add_test_disk, get_console_args, run_qemu


def create_test_disk(disk_path):
    if disk_path.exists():
        print(f"Test disk {disk_path} already exists. Skipping creation.")
        return

    print(f"Creating 1G test disk at {disk_path}...")

    subprocess.run(
        ["qemu-img", "create", "-f", "raw", str(disk_path), "1G"], check=True
    )

    subprocess.run(
        ["sgdisk", "--set-alignment=1", "--resize-table=4", str(disk_path)], check=True
    )
    subprocess.run(
        ["sgdisk", "--set-alignment=1", "--new=1:3:65365", str(disk_path)], check=True
    )
    subprocess.run(["sgdisk", "--typecode=1:ef00", str(disk_path)], check=True)
    subprocess.run(
        ["sgdisk", "--set-alignment=1", "--new=2:65536:0", str(disk_path)], check=True
    )
    subprocess.run(["sgdisk", "--typecode=2:8300", str(disk_path)], check=True)

    p1_sectors = 65365 - 3 + 1
    p1_size = p1_sectors * 512

    total_sectors = 1024 * 1024 * 1024 // 512
    p2_sectors = total_sectors - 65536 - 34
    p2_size = p2_sectors * 512

    p1_img = disk_path.with_suffix(".p1.tmp")
    p2_img = disk_path.with_suffix(".p2.tmp")

    try:
        print("Formatting Partition 1 (FAT16)...")
        p1_img.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["truncate", "-s", str(p1_size), str(p1_img)], check=True)
        subprocess.run(
            ["mkfs.fat", "-f1", "-F16", "-n", "BOOT0EFI", str(p1_img)], check=True
        )
        subprocess.run(
            [
                "dd",
                f"if={p1_img}",
                f"of={disk_path}",
                "bs=512",
                "seek=3",
                "conv=notrunc",
            ],
            check=True,
        )

        print("Formatting Partition 2 (EXT4)...")
        subprocess.run(["truncate", "-s", str(p2_size), str(p2_img)], check=True)
        subprocess.run(["mkfs.ext4", "-F", str(p2_img)], check=True)
        subprocess.run(
            [
                "dd",
                f"if={p2_img}",
                f"of={disk_path}",
                "bs=512",
                "seek=65536",
                "conv=notrunc",
            ],
            check=True,
        )

        print("Test disk created successfully.")
    except Exception as e:
        colors.error(f"Error creating test disk: {e}")
        if disk_path.exists():
            disk_path.unlink()
        raise
    finally:
        if p1_img.exists():
            p1_img.unlink()
        if p2_img.exists():
            p2_img.unlink()


def run_kernel(repos, arches, global_cfg, bld_base):
    """Run the built kernel under QEMU for the target architecture."""
    target_name = "linux-image"
    proj = next((r for r in repos if r["name"] == target_name), None)
    if not proj:
        print(f"Error: Component '{target_name}' not found.")
        sys.exit(1)

    if len(arches) > 1:
        print("Error: --run only supports one architecture at a time.")
        sys.exit(1)

    arch = arches[0]
    print(f"\n--- Run Stage: {target_name} ({arch}) ---")

    kernel_name = global_cfg.get("kernel_name", "kernel")
    kernel_img = bld_base / kernel_name
    if not kernel_img.exists():
        print(
            f"Error: Kernel image not found at {kernel_img}. "
            f"Please build it first with --build {target_name}."
        )
        sys.exit(1)

    try:
        cmd = get_qemu_command(arch, kernel_img, get_console_args(arch))
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    cmd = add_test_disk(cmd, bld_base / "test.img")

    try:
        run_qemu(cmd)
    except KeyboardInterrupt:
        print("\nKernel execution stopped by user.")
        sys.exit(1)
