# Una: Custom Musl-based Linux Build System

Una is a build tool designed to create a custom, minimal Linux system based on the musl C library. It manages the entire lifecycle of the build process, from fetching source code and building host tools to compiling a cross-architecture kernel and running it in emulation.

## Key Features

- **Multi-architecture support**: Target x32, x86_64, aarch64, and riscv64.
- **Automated orchestration**: Manages dependencies and build phases (Headers -> Runtime -> Base -> Kernel).
- **Embedded compiler configs**: Automatically generates architecture-specific Clang configurations (`musl.cfg`, etc.) to ensure correct header and library linking.
- **Git-integrated**: Automatic initialization, rebasing, and synchronization across multiple component repositories.
- **QEMU Integration**: Integrated emulator runner for testing kernels immediately after building.
- **Test Disk Support**: Easy creation of partitioned and formatted test disk images.

## Project Structure

- `una.py`: The main entry point for all operations.
- `repo/`: Contains sub-repositories for each component (musl, linux, openssl, nsd, etc.).
- `bld/`: Build artifacts, staging directories, and target images.
- `skel/`: Root filesystem skeleton that is propagated to target images.
- `confs/`: Default kernel and component configurations.
- `mods/`: Shared Python utilities used by the build system.

## Getting Started

### 1. Initialization
First, initialize the repository environment. This clones all necessary repositories and sets up the build directories.

```bash
python una.py --init [BASE_URL]
```
*Note: If you have a remote named `una` configured, it will attempt to detect the base URL automatically.*

### 2. Building

You can build the entire system or specific components for one or more architectures.

**Build everything for x32 (default):**
```bash
python una.py --build
```

**Build for specific architectures:**
```bash
python una.py --build --arch x86_64,aarch64
```

**Build a single component (e.g., nsd):**
```bash
python una.py --build nsd
```

#### Build Phases
Una executes builds in distinct phases to ensure dependencies are met:
1.  **Host Stage**: Builds necessary tools (like LLVM/Clang) if required.
2.  **Phase 0**: Installs System Headers (musl & linux).
3.  **Phase 1**: Builds and installs the core C library (musl).
4.  **Phase 2**: Builds Base Components (e.g., libmnl, libnftnl).
5.  **Phase 3**: Builds Other Components (e.g., openssl, nsd, dropbear).
6.  **Phase 4**: Kernel Finalization and Image Generation.

### 3. Running the Kernel

Una makes it easy to test your built kernel using QEMU.

**Run the default kernel (must be built first):**
```bash
python una.py --run --arch x32
```

### 4. Test Disk Management

For testing persistent storage, you can create a 1GB test disk image.

```bash
python una.py --create-disk
```
This creates `bld/test.img` with:
- A GPT partition table aligned to sector 3.
- **Partition 1**: EFI System Partition (FAT16), labeled `BOOT0EFI`.
- **Partition 2**: Linux Data Partition (EXT4).

If this disk exists, it is automatically attached to any QEMU run via `--run`.

## Component Development

Each component in `repo/` contains its own `una.py` build script that defines how it should be configured, built, and installed for the target architecture.

```python
# Example component structure
def target_configure(staging_dir, target_dir, arch):
    # logic to run ./configure with correct flags
    ...
```

The top-level `una.py` sets environment variables like `CFLAGS`, `CXXFLAGS`, and `LDFLAGS` to point to the architecture's specific `musl.cfg`, ensuring cross-compilation "just works".

## Maintenance Commands

- **Check Status**: `python una.py --status` (Show git status for all repos).
- **Save Changes**: `python una.py --save "message"` (Commit and tag changes across all repos).
- **Rebase**: `python una.py --rebase` (Fetch and rebase all local branches onto their remotes).
- **List Repos**: `python una.py --list all` (List all managed repositories).
