"""
container.py — A minimal container runtime in Python for educational purposes.
 
Demonstrates the three pillars of containerisation:
  1. Namespaces  — isolate what a process can *see*
  2. cgroups     — limit what a process can *use*
  3. Images      — control what filesystem the process *has*
 
Usage:
    # First, create a minimal "image" (a root filesystem tarball):
    #   Use the helper to export one from Docker:
    #     docker export $(docker create alpine:latest) -o alpine.tar.gz
    #
    # Then run a command inside your container:
    sudo python3 container.py run alpine.tar.gz /bin/sh
 
    # With resource limits:
    sudo python3 container.py run alpine.tar.gz /bin/sh --memory 64M --cpu 50
 
    # With a custom hostname:
    sudo python3 container.py run alpine.tar.gz /bin/sh --hostname my-container
 
Requires: Linux, root privileges, Python 3.10+
"""
 
import argparse
import ctypes
import os
import shutil
import sys
import tarfile
import tempfile
import uuid
 
 
# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
 
# Clone flags for unshare(2) — each one creates a new namespace type.
# These are the same flags Docker passes when it creates a container.
CLONE_NEWPID    = 0x20000000   # New PID namespace    → process gets its own PID tree
CLONE_NEWNS     = 0x00020000   # New mount namespace   → process gets its own filesystem mounts
CLONE_NEWUTS    = 0x04000000   # New UTS namespace     → process gets its own hostname
CLONE_NEWNET    = 0x40000000   # New network namespace → process gets its own network stack
CLONE_NEWIPC    = 0x08000000   # New IPC namespace     → process gets its own message queues
CLONE_NEWCGROUP = 0x02000000   # New cgroup namespace  → process gets its own cgroup view
 
# We don't create a new user namespace here because we're already root,
# and user namespaces add complexity that would obscure the core concepts.
ALL_NAMESPACES = (
    CLONE_NEWPID |
    CLONE_NEWNS  |
    CLONE_NEWUTS |
    CLONE_NEWNET |
    CLONE_NEWIPC |
    CLONE_NEWCGROUP
)
 
# Syscall numbers (x86_64 Linux)
SYS_UNSHARE    = 272
SYS_PIVOT_ROOT = 155
SYS_MOUNT      = 165
SYS_UMOUNT2    = 166
SYS_SETHOSTNAME = 170
 
libc = ctypes.CDLL("libc.so.6", use_errno=True)
 
 
# ---------------------------------------------------------------------------
# Low-level syscall wrappers
# ---------------------------------------------------------------------------
 
def unshare(flags: int) -> None:
    """
    unshare(2) — disassociate parts of the process execution context.
 
    This is the key syscall for namespace creation. Each flag tells the kernel
    to create a new namespace of that type for the calling process. After this
    call, the process (and any children it forks) will have an isolated view
    of the corresponding resource.
    """
    ret = libc.syscall(SYS_UNSHARE, flags)
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"unshare failed: {os.strerror(errno)}")
 
 
def pivot_root(new_root: str, put_old: str) -> None:
    """
    pivot_root(2) — change the root filesystem.
 
    This swaps the current root filesystem with new_root, and moves the
    old root to put_old. This is how containers get their own filesystem:
    the image contents become the new root, and the host filesystem is
    moved somewhere it can be unmounted.
 
    This is preferred over chroot because chroot only changes the path
    lookup root — processes can escape it. pivot_root actually changes
    the mount namespace's root.
    """
    ret = libc.syscall(
        SYS_PIVOT_ROOT,
        new_root.encode(),
        put_old.encode()
    )
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"pivot_root failed: {os.strerror(errno)}")
 
 
def mount(source: str, target: str, fstype: str, flags: int = 0, data: str = "") -> None:
    """Mount a filesystem. Thin wrapper around mount(2)."""
    ret = libc.syscall(
        SYS_MOUNT,
        source.encode(),
        target.encode(),
        fstype.encode(),
        flags,
        data.encode() if data else None,
    )
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"mount({source} -> {target}) failed: {os.strerror(errno)}")
 
 
def umount2(target: str, flags: int = 0) -> None:
    """Unmount a filesystem. Thin wrapper around umount2(2)."""
    ret = libc.syscall(SYS_UMOUNT2, target.encode(), flags)
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"umount2({target}) failed: {os.strerror(errno)}")
 
 
def sethostname(name: str) -> None:
    """Set the hostname inside the UTS namespace."""
    name_bytes = name.encode()
    ret = libc.syscall(SYS_SETHOSTNAME, name_bytes, len(name_bytes))
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"sethostname failed: {os.strerror(errno)}")
 
 
# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
 
def extract_image(image_path: str, target_dir: str) -> None:
    """
    "Pull" an image — in our case, just extract a tarball.
 
    A real container runtime like Docker uses a layered filesystem (overlayfs)
    where each layer in the image is stacked on top of the previous one.
    For simplicity, we just extract a flat tarball. The effect is the same:
    the container gets a root filesystem populated with the image contents.
    """
    print(f"  Extracting image: {image_path}")
    with tarfile.open(image_path) as tar:
        tar.extractall(path=target_dir)
    print(f"  Image extracted to: {target_dir}")
 
 
# ---------------------------------------------------------------------------
# Filesystem setup
# ---------------------------------------------------------------------------
 
MS_BIND       = 4096
MS_REC        = 16384
MS_PRIVATE    = 1 << 18
MNT_DETACH    = 2
 
def setup_filesystem(rootfs: str) -> None:
    """
    Prepare the container's root filesystem.
 
    This does three things:
    1. Makes the mount namespace private (so mounts don't leak to the host)
    2. Mounts /proc inside the new root (so tools like `ps` work)
    3. Uses pivot_root to swap the filesystem root to our image directory
 
    After this function, the process's "/" is the image contents and the
    host filesystem is no longer accessible.
    """
    # Make all existing mounts private to this namespace.
    # Without this, any mounts we create would propagate back to the host.
    mount("none", "/", "", MS_REC | MS_PRIVATE)
 
    # Bind-mount the new root onto itself. pivot_root requires the new root
    # to be a mount point.
    mount(rootfs, rootfs, "", MS_BIND | MS_REC)
 
    # Create /proc inside the new root and mount procfs there.
    # procfs is a virtual filesystem that exposes process information.
    # Without it, commands like `ps`, `top`, and reading /proc/self/*
    # won't work inside the container.
    proc_path = os.path.join(rootfs, "proc")
    os.makedirs(proc_path, exist_ok=True)
    mount("proc", proc_path, "proc")
 
    # Create a temporary directory inside the new root to stash the old root.
    old_root = os.path.join(rootfs, ".old_root")
    os.makedirs(old_root, exist_ok=True)
 
    # pivot_root: swap the filesystem root.
    # After this call:
    #   - rootfs becomes "/"
    #   - the old "/" is now at /.old_root
    pivot_root(rootfs, old_root)
 
    # Now change into the new root
    os.chdir("/")
 
    # Unmount and remove the old root — we don't want the container to have
    # any access to the host filesystem.
    umount2("/.old_root", MNT_DETACH)
    shutil.rmtree("/.old_root", ignore_errors=True)
 
 
# ---------------------------------------------------------------------------
# cgroup setup
# ---------------------------------------------------------------------------
 
def setup_cgroup(
    container_id: str,
    memory_limit: str | None = None,
    cpu_percent: int | None = None,
) -> str:
    """
    Create a cgroup for the container and apply resource limits.
 
    cgroups (control groups) are the kernel mechanism for limiting resources.
    They work through a virtual filesystem at /sys/fs/cgroup. To limit a
    process's resources, you:
      1. Create a directory (the cgroup)
      2. Write limits to files in that directory
      3. Write the process's PID to the cgroup's `cgroup.procs` file
 
    The kernel then enforces those limits on all processes in the cgroup.
    """
    cgroup_path = f"/sys/fs/cgroup/minicontainer-{container_id}"
 
    # Create the cgroup directory
    os.makedirs(cgroup_path, exist_ok=True)
 
    if memory_limit:
        # Parse human-readable memory limits like "64M" or "512K"
        multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
        suffix = memory_limit[-1].upper()
        if suffix in multipliers:
            limit_bytes = int(memory_limit[:-1]) * multipliers[suffix]
        else:
            limit_bytes = int(memory_limit)
 
        # Write the memory limit. The kernel will kill any process in this
        # cgroup that tries to exceed this limit (OOM kill).
        limit_file = os.path.join(cgroup_path, "memory.max")
        with open(limit_file, "w") as f:
            f.write(str(limit_bytes))
        print(f"  Memory limit: {memory_limit} ({limit_bytes} bytes)")
 
    if cpu_percent is not None:
        # CPU limits in cgroups v2 use a "quota/period" model.
        # The period is a time window (default 100ms = 100000µs).
        # The quota is how many µs of CPU time the cgroup can use per period.
        # So 50% CPU = 50000µs quota in a 100000µs period.
        period = 100_000  # microseconds
        quota = int(period * (cpu_percent / 100))
        cpu_file = os.path.join(cgroup_path, "cpu.max")
        with open(cpu_file, "w") as f:
            f.write(f"{quota} {period}")
        print(f"  CPU limit: {cpu_percent}% ({quota}/{period} µs)")
 
    return cgroup_path
 
 
def add_process_to_cgroup(cgroup_path: str, pid: int) -> None:
    """
    Add a process to a cgroup.
 
    Once a PID is written to cgroup.procs, the kernel enforces all of the
    cgroup's limits on that process (and its children).
    """
    procs_file = os.path.join(cgroup_path, "cgroup.procs")
    with open(procs_file, "w") as f:
        f.write(str(pid))
 
 
def cleanup_cgroup(cgroup_path: str) -> None:
    """Remove the cgroup directory when the container exits."""
    try:
        os.rmdir(cgroup_path)
    except OSError:
        pass
 
 
# ---------------------------------------------------------------------------
# Container entry point
# ---------------------------------------------------------------------------
 
def run_container(
    image_path: str,
    command: list[str],
    hostname: str = "container",
    memory_limit: str | None = None,
    cpu_percent: int | None = None,
) -> None:
    """
    Run a command inside a container.
 
    This is the main orchestration function. It mirrors (in simplified form)
    what Docker does when you run `docker run`:
 
      1. Extract the image → like `docker pull`
      2. Create namespaces → isolate what the process can see
      3. Set up cgroups → limit what the process can use
      4. Set up the filesystem → give the process the image as its root
      5. Execute the command → the "entrypoint"
    """
    container_id = uuid.uuid4().hex[:12]
    rootfs = tempfile.mkdtemp(prefix=f"container-{container_id}-")
 
    print(f"Starting container {container_id}")
 
    # Step 1: Extract the image into a temporary directory.
    # This becomes the container's root filesystem.
    extract_image(image_path, rootfs)
 
    # Step 2: Set up cgroups *before* we fork, so we can add the child
    # process to the cgroup immediately.
    cgroup_path = None
    if memory_limit or cpu_percent:
        print("  Setting up cgroups...")
        cgroup_path = setup_cgroup(container_id, memory_limit, cpu_percent)
 
    # Step 3: Create new namespaces.
    # unshare() tells the kernel: "from now on, give me (and my children)
    # isolated versions of these resources."
    print("  Creating namespaces...")
    unshare(ALL_NAMESPACES)
 
    # Step 4: Fork. The child will become the containerised process.
    # We need to fork *after* CLONE_NEWPID so the child gets PID 1
    # in the new PID namespace. The parent stays in the original namespace
    # to manage cleanup.
    pid = os.fork()
 
    if pid == 0:
        # ---- CHILD PROCESS (this is the "container") ----
 
        # Set the hostname inside the UTS namespace.
        # This only affects this namespace — the host's hostname is untouched.
        sethostname(hostname)
 
        # Set up the filesystem: mount the image as root, mount /proc, etc.
        setup_filesystem(rootfs)
 
        # Replace this process with the requested command.
        # After this, the container is "running" — it's just a process with
        # a restricted view of the world.
        print(f"  Executing: {' '.join(command)}")
        print("=" * 60)
        os.execvp(command[0], command)
 
    else:
        # ---- PARENT PROCESS (manages the container lifecycle) ----
 
        # Add the child to the cgroup so resource limits are enforced.
        if cgroup_path:
            add_process_to_cgroup(cgroup_path, pid)
 
        # Wait for the container process to exit.
        _, status = os.waitpid(pid, 0)
 
        # Clean up.
        print("=" * 60)
        print(f"Container {container_id} exited with status {os.WEXITSTATUS(status)}")
        if cgroup_path:
            cleanup_cgroup(cgroup_path)
        shutil.rmtree(rootfs, ignore_errors=True)
 
 
# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
 
def main() -> None:
    parser = argparse.ArgumentParser(
        description="minicontainer — a tiny container runtime for learning",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run a shell in an Alpine container:
  sudo python3 container.py run alpine.tar.gz /bin/sh
 
  # Run with a 64MB memory limit and 50% CPU cap:
  sudo python3 container.py run alpine.tar.gz /bin/sh --memory 64M --cpu 50
 
  # Creating an image from Docker:
  docker export $(docker create alpine:latest) -o alpine.tar.gz
        """,
    )
    sub = parser.add_subparsers(dest="action")
 
    run_parser = sub.add_parser("run", help="Run a command in a new container")
    run_parser.add_argument("image", help="Path to image tarball (e.g. alpine.tar.gz)")
    run_parser.add_argument("command", nargs="+", help="Command to execute")
    run_parser.add_argument("--hostname", default="container", help="Container hostname")
    run_parser.add_argument("--memory", default=None, help="Memory limit (e.g. 64M, 512K)")
    run_parser.add_argument("--cpu", type=int, default=None, help="CPU limit as percentage (e.g. 50)")
 
    args = parser.parse_args()
 
    if args.action != "run":
        parser.print_help()
        sys.exit(1)
 
    if os.geteuid() != 0:
        print("Error: minicontainer must be run as root.")
        print("Try: sudo python3 container.py run ...")
        sys.exit(1)
 
    run_container(
        image_path=args.image,
        command=args.command,
        hostname=args.hostname,
        memory_limit=args.memory,
        cpu_percent=args.cpu,
    )
 
 
if __name__ == "__main__":
    main()
