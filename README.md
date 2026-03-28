# pycrate

A minimal container runtime written in Python for educational purposes only. It demonstrates the three Linux features that make containers work: **namespaces**, **cgroups**, and **chroot**.

The implementation is most likely full of all kinds of security holes so don't use it for anything you care about.

## Usage

pycrate must run on Linux as root.

```bash
cd /pycrate
python3 pycrate.py run <image.tar.gz> <command> [options]
```

### Example

```bash
# Run a shell inside a container with resource limits
sudo ./pycrate.py run ubuntu.tar.gz /bin/bash --hostname mycontainer --memory 64 --cpu 50
```

### Options

| Flag         | Description                  | Example |
| ------------ | ---------------------------- | ------- |
| `--hostname` | Set the container's hostname | `mybox` |
| `--memory`   | Memory limit in MB           | `64`    |
| `--cpu`      | CPU limit as a percentage    | `50`    |

## How it works

When you run `pycrate.py run`, it goes through four steps that mirror what Docker does with `docker run`:

### Step 1: Extract the image

```
ubuntu.tar.gz  ->  /tmp/container-<id>/
                      |-- bin/
                      |-- etc/
                      |-- lib/
                      |-- proc/
                      |-- ...
```

The image tarball is extracted into a temporary directory. This becomes the container's root filesystem. A real container runtime like Docker uses a layered filesystem (overlayfs), but a flat tarball achieves the same result for our purposes. The container gets a populated filesystem to run in.

### Step 2: Set up cgroups (optional)

cgroups (control groups) are the kernel mechanism for limiting resources. They work through a virtual filesystem at `/sys/fs/cgroup`. To limit a process:

1. Create a directory under `/sys/fs/cgroup/` (this is the cgroup)
2. Write limits to files in that directory (`memory.max`, `cpu.max`)
3. Write the process's PID to `cgroup.procs`

The kernel then enforces those limits. If a process exceeds its memory limit, the kernel OOM kills it. CPU limits use a quota/period model e.g. 50% CPU means the process gets 50ms of CPU time per 100ms window.

The current process is added to the cgroup before namespaces are created, so the forked child inherits membership automatically.

### Step 3: Create namespaces

```python
os.unshare(CLONE_NEWPID | CLONE_NEWNS | CLONE_NEWUTS | CLONE_NEWNET | CLONE_NEWIPC)
```

`unshare()` tells the kernel to give this process and its children isolated versions of system resources. Each namespace type isolates something different:

| Namespace | Flag           | What it isolates                                    |
| --------- | -------------- | --------------------------------------------------- |
| PID       | `CLONE_NEWPID` | Process IDs (container sees only its own processes) |
| Mount     | `CLONE_NEWNS`  | Mount table (container's mounts don't affect host)  |
| UTS       | `CLONE_NEWUTS` | Hostname (container gets its own hostname)          |
| Network   | `CLONE_NEWNET` | Network stack (container gets its own interfaces)   |
| IPC       | `CLONE_NEWIPC` | Inter-process communication (shared memory, etc.)   |

### Step 4: Fork and set up the filesystem

After creating namespaces, the process forks:

```
          os.fork()
          /         \
  Child (pid=0)    Parent
      |               |
sethostname()     waitpid(child)
      |               |
setup_filesystem()  cleanup()
      |
os.execvp(command)
```

**The child** becomes the container:

1. **Sets the hostname** inside the UTS namespace using `socket.sethostname()`
2. **Mounts `/proc`** inside the new root - a virtual filesystem that exposes process info. Without it, `ps` and `top` won't work.
3. **Calls `chroot(rootfs)`** this tells the kernel "for this process, `/` now means `rootfs`." The container can only see files inside the extracted image.
4. **Calls `os.execvp(command)`** replaces itself with the requested command (e.g. `/bin/bash`). The container is now "running."

**The parent** manages the lifecycle:

1. Waits for the child to exit
2. Moves itself out of the cgroup and removes it
3. Deletes the temporary rootfs directory

## Limitations

This is an educational tool, not a production container runtime. Some important differences from something like Docker:

- **chroot instead of pivot_root** - `chroot` is a path-lookup trick that a root process can escape. Docker uses `pivot_root` which actually swaps the mount namespace root, making escape much harder.
- **No overlayfs** - Docker uses layered filesystems so multiple containers can share base image layers. We extract a flat tarball each time.
- **No networking** - We create a network namespace but don't set up virtual interfaces, so the container has no network access.
- **No cgroup namespace** - We skip `CLONE_NEWCGROUP` because our single-process architecture (unlike Docker's daemon model) means the parent needs access to `/sys/fs/cgroup` for cleanup after the container exits.
- **Must run as root** - Docker's daemon handles privilege separation. We require root directly.
