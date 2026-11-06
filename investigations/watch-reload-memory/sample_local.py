#!/usr/bin/env python3
"""Sample one local Docker Zigflow process across watch reloads.

The workflow and reloader paths must be local bind-mounted files. This script
does not use kubectl or contact any Kubernetes environment.
"""

from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import time

METRICS = (
    "go_memstats_heap_alloc_bytes",
    "go_memstats_heap_objects",
    "go_memstats_sys_bytes",
    "go_memstats_heap_released_bytes",
    "go_gc_duration_seconds_count",
)


def docker_exec(container: str, *args: str) -> str:
    return subprocess.check_output(["docker", "exec", container, *args], text=True)


def snapshot(container: str) -> dict[str, str | int | float]:
    metrics = docker_exec(container, "wget", "-qO-", "http://127.0.0.1:9090/metrics")
    values = {}
    for line in metrics.splitlines():
        name, _, value = line.partition(" ")
        if name in METRICS:
            values[name] = float(value)
    missing = set(METRICS) - values.keys()
    if missing:
        raise RuntimeError(f"metrics endpoint is missing: {sorted(missing)}")

    status = docker_exec(container, "cat", "/proc/1/status")
    rss_kib = int(next(line.split()[1] for line in status.splitlines() if line.startswith("VmRSS:")))
    return {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "rss_bytes": rss_kib * 1024,
        **values,
    }


def rewrite(path: Path) -> None:
    content = path.read_bytes()
    with path.open("wb", buffering=0) as stream:
        stream.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True, help="name of the local Docker container")
    parser.add_argument("--workflow", required=True, type=Path, help="bind-mounted workflow file")
    parser.add_argument("--reloader", required=True, type=Path, help="bind-mounted reloader workflow")
    parser.add_argument("--output", required=True, type=Path, help="CSV result path")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--settle-seconds", type=int, default=40)
    args = parser.parse_args()

    for path in (args.workflow, args.reloader):
        if not path.is_file():
            parser.error(f"not a regular local file: {path}")
    if args.cycles < 1 or args.settle_seconds < 5 or args.settle_seconds % 5:
        parser.error("cycles must be positive and settle-seconds must be a multiple of five")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fields = ["time_utc", "cycle", "phase", "rss_bytes", *METRICS]
    with args.output.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for cycle in range(args.cycles):
            for phase in ("before", "workflow-write", "reloader-write"):
                if phase == "workflow-write":
                    rewrite(args.workflow)
                elif phase == "reloader-write":
                    time.sleep(2)
                    rewrite(args.reloader)
                row = snapshot(args.container)
                writer.writerow({"cycle": cycle, "phase": phase, **row})
                stream.flush()
                print(cycle, phase, "RSS MiB", round(row["rss_bytes"] / 1048576, 1),
                      "Go heap MiB", round(row["go_memstats_heap_alloc_bytes"] / 1048576, 1), flush=True)
            for elapsed in range(5, args.settle_seconds + 1, 5):
                time.sleep(5)
                row = snapshot(args.container)
                writer.writerow({"cycle": cycle, "phase": f"after-{elapsed}s", **row})
                stream.flush()
                print(cycle, f"after-{elapsed}s", "RSS MiB", round(row["rss_bytes"] / 1048576, 1),
                      "Go heap MiB", round(row["go_memstats_heap_alloc_bytes"] / 1048576, 1), flush=True)


if __name__ == "__main__":
    main()
