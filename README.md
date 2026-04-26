# Una: Custom Musl-based Linux Build System

Una is a build tool designed to create a custom, minimal Linux system based on the musl C library. It manages the entire lifecycle of the build process, from fetching source code and building host tools to compiling a cross-architecture kernel and running it in emulation.

## Key Features

- **Build Verification**: Automatic snapshotting and hashing of build artifacts to track incremental changes.
- **Automated Orchestration**: Manages dependencies and build phases (Headers -> Runtime -> Base -> Kernel).
- **Safe Environment**: Prevents accidental data loss by blocking `git clean` operations on repositories with uncommitted changes.
- **Embedded compiler configs**: Automatically generates architecture-specific Clang configurations (`musl.cfg`, etc.) to ensure correct header and library linking.
- **Git-integrated**: Automatic initialization, rebasing, and synchronization across multiple component repositories.
- **QEMU Integration**: Integrated emulator runner for testing kernels immediately after building.
- **Test Disk Support**: Easy creation of partitioned and formatted test disk images.

## Project Structure

- `una.py`: The main entry point for all operations.
- `repo/`: Contains sub-repositories for each component (musl, linux, openssl, nsd, etc.).
- `bld/`: Build artifacts, staging directories, and target images.
  - `bld/[arch]/report/`: contains detailed change reports for each component.
- `skel/`: Root filesystem skeleton that is propagated to target images.
- `confs/`: Default kernel and component configurations.
- `mods/`: Shared Python utilities (snapshotting, git helpers, etc.).

## Getting Started

### 1. Initialization
Initialization happens automatically when you run any command. Una will detect missing repositories from your configuration and clone them.

If your top-level repository has a remote named `una`, Una will automatically use that as the base URL for component repositories.

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

### 3. Build Verification & Reporting

Una includes a robust verification system that monitors the build environment for every component:

- **Automatic Clear**: Before building a component, Una uses previous reports to automatically "clear" (remove) files it previously installed. This ensures a clean slate for incremental builds without rebuilding everything from scratch.
- **Change Reporting**: After each step, Una generates a report in `bld/[arch]/report/[component].txt`. This report lists:
    - **A**: Added files/directories.
    - **C/M/P**: Modified content, metadata, or permissions.
    - **D**: Deleted files.
- **Integrity Checks**: If a component unexpectedly modifies or deletes files outside of its own previous scope (e.g., touching files from another component), Una will report an error to help track down build issues.

### 4. Running the Kernel

Una makes it easy to test your built kernel using QEMU.

**Run the default kernel (must be built first):**
```bash
python una.py --run --arch x32
```

### 5. Test Disk Management

For testing persistent storage, you can create a 1GB test disk image.

```bash
python una.py --create-disk
```
This creates `bld/test.img` with a GPT partition table and formatted FAT/EXT4 partitions. If this disk exists, it is automatically attached to any QEMU run via `--run`.

## Component Development

Each component in `repo/` contains its own `una.py` build script that defines how it should be configured, built, and installed for the target architecture.

The top-level `una.py` sets environment variables like `CFLAGS`, `CXXFLAGS`, and `LDFLAGS` to point to the architecture's specific `musl.cfg`, ensuring cross-compilation "just works".

## Maintenance Commands

- **Check Status**: `python una.py --status` (Show git status for all repos).
- **Save Changes**: `python una.py --save "tag"` (Stage, commit, tag, and push changes across all repos).
- **Checkout Tag**: `python una.py --checkout "tag"` (Switch all repositories to a specific tag).
- **Rebase**: `python una.py --rebase` (Fetch and rebase local branches onto upstream branches with squashing for a clean history).
- **Change Report**: `python una.py --report` (Generate a summary of all local changes in the `bld/report/` directory).
- **Clean**: `python una.py --clean` (Global cleanup of build artifacts and environment; safe check for dirty repos).
- **List Repos**: `python una.py --list [type]` (List managed repositories).
  - Use `all` to see everything.
  - Use `host`, `base`, or `other` to filter by type.

### Component Types

When listing or configuring repositories, they are categorized into three main types:

*   **host**: These are tools that run on your computer to help build the system (like compilers). They are built first.
*   **base**: These are the fundamental parts of the target system, like the C library (`musl`) and system headers. They form the foundation that everything else sits on.
*   **other**: these are the "extra" parts of the system, like applications (BusyBox), security tools (OpenSSL), or the final kernel image itself.

## How Repositories are Specified

Repositories are defined in configuration files (like `confs/default.conf`). You don't need to be a programmer to understand or edit these. Each repository has its own section:

```ini
[busybox]                # The name of the component
una_repo = busybox.git   # Where it lives on the server
repo_dir = repo/busybox  # Where it will be stored on your computer
type = other             # What kind of component it is (host/base/other)
tag = 1_37_0             # (Optional) A specific version to use
```

You can also make one component "copy" settings from another using `ref = name`. This is useful for things like the Linux kernel, where you might have one entry for the "headers" and another for the "image", both sharing the same source code.
108: 
109: ## Advanced Configuration
110: 
111: ### Repository Tags
112: In `confs/default.conf`, you can specify a specific tag for any repository. When initialized or reset, Una will checkout this exact tag instead of the default `una` branch.
113: 
114: ```ini
115: [musl]
116: una_repo = musl.git
117: repo_dir = repo/musl
118: origin_url = https://git.musl-libc.org/git/musl
119: tag = v1.2.5
120: ```
121: 
122: ### Workflow: Rebase and Squash
123: To maintain a clean history, Una's `--rebase` and `--save` commands use a squashing workflow. Local commits are squashed into a single logical update commit on top of the upstream base before force-pushing. This allows the `una` branch to remain a flat series of project-specific patches.
