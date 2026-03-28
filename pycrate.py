#!/usr/bin/env python3

"""
A minimal container runtime in Python for educational purposes.

Demonstrates three important components of containerisation:
  1. Namespaces - isolate what a process can see
  2. cgroups - limit what a process can use
  3. Images - control what filesystem the process has
"""

import argparse
import os
import shutil
import socket
import sys
import tempfile
import uuid
from argparse import Namespace

from utils.cgroups import add_process_to_cgroup, cleanup_cgroup, setup_cgroup
from utils.filesystem import setup_filesystem
from utils.images import extract_image

# We don't create a new user namespace here because we're already root,
# and user namespaces add complexity.
ALL_NAMESPACES = (
    os.CLONE_NEWPID
    | os.CLONE_NEWNS
    | os.CLONE_NEWUTS
    | os.CLONE_NEWNET
    | os.CLONE_NEWIPC
    | os.CLONE_NEWCGROUP
)


def run_container(
    image_path: str,
    command: list[str],
    hostname: str = "container",
    memory_mb: int | None = None,
    cpu_percent: int | None = None,
) -> None:
    """
    Run a command inside a container.

    This is the main orchestration function. It mirrors (in simplified form)
    what Docker does when you run `docker run`:

      1. Extract the image - like `docker pull`
      2. Set up cgroups - limit what the process can use
      3. Create namespaces - isolate what the process can see
      4. Fork the process and set up the filesystem - give the process the image as its root
    """
    container_id = uuid.uuid4().hex
    rootfs = tempfile.mkdtemp(prefix=f"container-{container_id}-")

    print(f"Starting container {container_id}")

    # Step 1: Extract the image into a temporary directory.
    # This becomes the container's root filesystem.
    extract_image(image_path, rootfs)

    # Step 2: Set up cgroups before we fork, so we can add the child
    # process to the cgroup immediately.
    cgroup_path = None
    if memory_mb or cpu_percent:
        print("Setting up cgroups...")
        cgroup_path = setup_cgroup(container_id, memory_mb, cpu_percent)

    # Step 3: Create new namespaces.
    # unshare() tells the kernel: "from now on, give me and my children
    # isolated versions of these resources."
    print("Creating namespaces...")
    os.unshare(ALL_NAMESPACES)

    # Step 4: Fork. The child will become the containerised process.
    # We need to fork after CLONE_NEWPID so the child gets PID 1
    # in the new PID namespace. The parent stays in the original namespace
    # to manage cleanup.
    pid = os.fork()

    if pid == 0:
        # Child process: the container
        # Set the hostname inside the UTS namespace.
        socket.sethostname(hostname)

        # Set up the filesystem: mount the image as root, mount /proc, etc.
        setup_filesystem(rootfs)

        print(f"Executing: {' '.join(command)}")
        print()
        os.execvp(command[0], command)

    else:
        # Parent process: manages the container lifecycle

        # Add the child to the cgroup so resource limits are enforced.
        if cgroup_path:
            add_process_to_cgroup(cgroup_path, pid)

        # Wait for the container process to exit.
        _, status = os.waitpid(pid, 0)

        # Clean up.
        if cgroup_path:
            cleanup_cgroup(cgroup_path)
        shutil.rmtree(rootfs, ignore_errors=True)


def parse_args() -> Namespace:
    parser = argparse.ArgumentParser(
        description="pycrate - a minimal container runtime for learning",
    )

    sub = parser.add_subparsers(dest="action")

    run_parser = sub.add_parser("run", help="Run a command in a new container")
    run_parser.add_argument("image", help="Path to image tarball (e.g. alpine.tar.gz)")
    run_parser.add_argument("command", nargs="+", help="Command to execute")
    run_parser.add_argument(
        "--hostname", default="container", help="Container hostname"
    )
    run_parser.add_argument(
        "--memory", type=int, default=None, help="Memory limit in MB (e.g. 64)"
    )
    run_parser.add_argument(
        "--cpu", type=int, default=None, help="CPU limit as percentage (e.g. 50)"
    )

    args = parser.parse_args()

    if args.action != "run":
        print("Error: No action specified.")
        sys.exit(1)

    if os.geteuid() != 0:
        print("Error: pycrate must be run as root.")
        print("Try: sudo python3 pycrate.py run ...")
        sys.exit(1)

    return args


def main() -> None:
    args = parse_args()

    run_container(
        image_path=args.image,
        command=args.command,
        hostname=args.hostname,
        memory_mb=args.memory,
        cpu_percent=args.cpu,
    )


if __name__ == "__main__":
    main()
