import os


def setup_cgroup(
    container_id: str,
    memory_mb: int | None = None,
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
    cgroup_path = f"/sys/fs/cgroup/pycrate-{container_id}"

    # Create the cgroup directory
    os.makedirs(cgroup_path, exist_ok=True)

    if memory_mb is not None:
        limit_bytes = memory_mb * 1024 * 1024

        # Write the memory limit. The kernel will kill any process in this
        # cgroup that tries to exceed this limit (OOM kill).
        limit_file = os.path.join(cgroup_path, "memory.max")
        with open(limit_file, "w") as f:
            f.write(str(limit_bytes))
        print(f"Memory limit: {memory_mb}MB")

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
        print(f"CPU limit: {cpu_percent}% ({quota}/{period} µs)")

    return cgroup_path


def add_process_to_cgroup(cgroup_path: str, pid: int) -> None:
    """
    Add a process to a cgroup.

    Once a PID is written to cgroup.procs, the kernel enforces all of the
    cgroup's limits on that process and its children.
    """
    procs_file = os.path.join(cgroup_path, "cgroup.procs")
    with open(procs_file, "w") as f:
        f.write(str(pid))


def cleanup_cgroup(cgroup_path: str) -> None:
    os.rmdir(cgroup_path)
