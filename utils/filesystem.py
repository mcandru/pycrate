import os
import subprocess


def setup_filesystem(rootfs: str) -> None:
    """
    Prepare the container's root filesystem.

    This does three things:
    1. Mounts /proc inside the new root (so tools like `ps` work)
    2. Uses chroot to make the image directory the new root
    3. Changes into the new root directory

    After this function, the process's "/" is the image contents and the
    host filesystem is no longer accessible.
    """
    # Mount /proc inside the new root. procfs is a virtual filesystem that
    # exposes process information. Without it, commands like `ps` and `top`
    # won't work inside the container.
    proc_path = os.path.join(rootfs, "proc")
    os.makedirs(proc_path, exist_ok=True)
    subprocess.run(["mount", "-t", "proc", "proc", proc_path], check=True)

    # chroot: make the image directory the new root filesystem.
    # After this, all path lookups start from rootfs instead of the real "/".
    # IMPORTANT: this is vulnerable to chroot escapes if the image contains malicious binaries. In a
    # real container runtime, you'd use additional isolation (like user namespaces) to mitigate this.
    os.chroot(rootfs)
    os.chdir("/")
