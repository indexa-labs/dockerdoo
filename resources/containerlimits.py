#!/usr/bin/env python3
"""Print shell exports that fit Odoo's HTTP thread pool and memory limits to the container.

Odoo's defaults assume a dedicated host. In threaded mode every open websocket holds one HTTP thread, and the pool
is 2 x CPU + 1, counted on the host's CPUs whatever the container may use. Odoo's memory limits default to 2048 and
2560 MiB and measure virtual size, which includes the 8 MiB stack that every thread reserves. A variable that the
environment already sets is left alone, and so is everything on a machine of 16 GiB or more, which is the dedicated
host Odoo's defaults were chosen for.
"""

import os
import shlex
from pathlib import Path

MiB = 1024 * 1024
THREAD_STACK = 8 * MiB
CGROUP_LIMITS = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
DEDICATED_HOST = 16 * 1024 * MiB


def memory_limit(cgroup_files=CGROUP_LIMITS, meminfo="/proc/meminfo"):
    """Bytes the container can use: its cgroup limit or, without one (virtual machines), the machine's memory."""
    limits = []
    for path in cgroup_files:
        try:
            text = Path(path).read_text().strip()
        except OSError:
            continue
        if text.isdigit():
            limits.append(int(text))
    try:
        for line in Path(meminfo).read_text().splitlines():
            if line.startswith("MemTotal:"):
                limits.append(int(line.split()[1]) * 1024)
    except OSError:
        pass
    return min(limits) if limits else None


def derive(environ, memory):
    """The settings the environment leaves unset, for a container that may use this many bytes."""
    if not memory or memory >= DEDICATED_HOST:
        return {}
    derived = {}
    if not environ.get("ODOO_MAX_HTTP_THREADS"):
        # A user holds a thread per open tab, so allow a thread per 16 MiB.
        derived["ODOO_MAX_HTTP_THREADS"] = str(min(256, max(32, memory // (16 * MiB))))
    if environ.get("ODOO_WORKERS", "0") in ("", "0"):
        # Worker processes have limits of their own; this is the one process serving everything.
        stacks = int(environ.get("ODOO_MAX_HTTP_THREADS") or derived["ODOO_MAX_HTTP_THREADS"]) * THREAD_STACK
        for name, share in (("ODOO_LIMIT_MEMORY_SOFT", 0.70), ("ODOO_LIMIT_MEMORY_HARD", 0.90)):
            if name not in environ:
                # The rest of the container, such as wkhtmltopdf, needs memory too.
                derived[name] = str(int(memory * share) + stacks)
    return derived


if __name__ == "__main__":
    for name, value in sorted(derive(os.environ, memory_limit()).items()):
        print(f"export {name}={shlex.quote(value)}")
