```python
"""Simple Docker Container Health Monitor for cloud/DevOps engineers."""

import argparse
import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


class ContainerInfo:
    """Represents a Docker container and its health status."""

    FIELDS = [
        "id", "name", "image", "status", "state",
        "cpu_percent", "memory_usage_mb", "memory_limit_mb",
        "memory_percent", "restart_count", "healthy"
    ]

    def __init__(self, **kwargs: Any) -> None:
        self.id: str = kwargs.get("id", "unknown")
        self.name: str = kwargs.get("name", "unknown")
        self.image: str = kwargs.get("image", "unknown")
        self.status: str = kwargs.get("status", "unknown")
        self.state: str = kwargs.get("state", "unknown")
        self.cpu_percent: float = float(kwargs.get("cpu_percent", 0.0))
        self.memory_usage_mb: float = float(kwargs.get("memory_usage_mb", 0.0))
        self.memory_limit_mb: float = float(kwargs.get("memory_limit_mb", 0.0))
        self.memory_percent: float = float(kwargs.get("memory_percent", 0.0))
        self.restart_count: int = int(kwargs.get("restart_count", 0))
        self.healthy: bool = kwargs.get("healthy", False)

    def to_dict(self) -> dict[str, Any]:
        return {f: getattr(self, f) for f in self.FIELDS}

    def __repr__(self) -> str:
        return f"<ContainerInfo {self.name!r} ({self.state})>"


def run_docker_command(args: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """Execute a Docker CLI command and return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            ["docker"] + args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return 1, "", "Docker CLI not found. Is Docker installed and in PATH?"
    except subprocess.TimeoutExpired:
        return 1, "", f"Command timed out after {timeout}s: docker {' '.join(args)}"
    except Exception as exc:
        return 1, "", str(exc)


def fetch_container_details(container_id: str, timeout: int = 10) -> dict[str, Any]:
    """Fetch CPU and memory stats for a single container."""
    cmd = [
        "stats", "--no-stream", "--format",
        ("{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}|{{.Name}}|{{.ID}}"),
        container_id
    ]
    rc, stdout, stderr = run_docker_command(cmd, timeout=timeout)
    if rc != 0:
        logger.warning("Stats failed for %s: %s", container_id, stderr)
        return {}
    parts = stdout.split("|")
    if len(parts) < 4:
        return {}
    try:
        cpu_str = parts[0].replace("%", "").strip()
        mem_parts = parts[1].split("/")
        mem_used = float(mem_parts[0].replace("MiB", "").strip()) if len(mem_parts) > 0 else 0.0
        mem_total = float(mem_parts[1].replace("MiB", "").strip()) if len(mem_parts) > 1 else 0.0
        mem_pct_str = parts[2].replace("%", "").strip()
        return {
            "cpu_percent": float(cpu_str) if cpu_str else 0.0,
            "memory_usage_mb": mem_used,
            "memory_limit_mb": mem_total,
            "memory_percent": float(mem_pct_str) if mem_pct_str else 0.0,
            "name": parts[3].strip(),
        }
    except (ValueError, IndexError) as exc:
        logger.warning("Failed to parse stats for %s: %s", container_id, exc)
        return {}


def get_container_status(container_id: str, timeout: int = 10) -> dict[str, Any]:
    """Fetch health and metadata for a single container."""
    fmt = (
        "index={{.Status}}"
        "|state={{.State}}"
        "|health={{.Health.Status}}"
        "|id={{.ID}}"
        "|image={{.Image}}"
        "|restarts={{.RestartCount}}"
    )
    cmd = ["inspect", "--format", fmt, container_id]
    rc, stdout, stderr = run_docker_command(cmd, timeout=timeout)
    if rc != 0:
        return {}
    result: dict[str, Any] = {}
    for part in stdout.split("|"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        result[key] = value
    return result


def list_container_ids(container_filter: str | None = None) -> list[str]:
    """Return list of container IDs, optionally filtered by label or name."""
    args = ["ps", "--format", "{{.ID}}"]
    if container_filter:
        args.extend(["--filter", f"label={container_filter}"])
    rc, stdout, stderr = run_docker_command(args)
    if rc != 0:
        logger.error("Failed to list containers: %s", stderr)
        return []
    return [cid.strip() for cid in stdout.splitlines() if cid.strip()]


def monitor_containers(
    container_filter: str | None,
    healthy_only: bool,
    unhealthy_only: bool,
    output_format: str,
    sort_by: str,
    reverse_sort: bool,
    threshold_cpu: float,
    threshold_memory: float,
) -> int:
    """Core monitoring logic: collect status, apply filters, output results."""
    container_ids = list_container_ids(container_filter)
    if not container_ids:
        print("No containers found.", file=sys.stderr)
        return 1

    containers: list[ContainerInfo] = []
    failed_lookups: list[str] = []

    for cid in container_ids:
        status_data = get_container_status(cid)
        if not status_data:
            failed_lookups.append(cid)
            continue
        stats_data = fetch_container_details(cid)
        name = stats_data.get("name", status_data.get("name", cid[:12]))
        containers.append(ContainerInfo(
            id=cid[:12],
            name=name,
            image=status_data.get("image", "unknown"),
            status=status_data.get("status", "unknown"),
            state=status_data.get("state", "unknown"),
            cpu_percent=stats_data.get("cpu_percent", 0.0),
            memory_usage_mb=stats_data.get("memory_usage_mb", 0.0),
            memory_limit_mb=stats_data.get("memory_limit_mb", 0.0),
            memory_percent=stats_data.get("memory_percent", 0.0),
            restart_count=int(status_data.get("restarts", 0)),
            healthy=status_data.get("health", "") == "healthy",
        ))

    for failed in failed_lookups:
        logger.warning("Could not inspect container: %s", failed)

    # Apply health filters
    if healthy_only:
        containers = [c for c in containers if c.healthy]
    if unhealthy_only:
        containers = [c for c in containers if not c.healthy]

    # Apply resource thresholds
    if threshold_cpu > 0 or threshold_memory > 0:
        containers = [
            c for c in containers
            if c.cpu_percent >= threshold_cpu or c.memory_percent >= threshold_memory
        ]

    # Sorting
    if sort_by and sort_by in {f for f in ContainerInfo.FIELDS}:
        containers.sort(key=lambda c: getattr(c, sort_by), reverse=reverse_sort)

    # Output
    if output_format == "json":
        print(json.dumps([c.to_dict() for c in containers], indent=2))
    else:
        _print_table(containers, threshold_cpu, threshold_memory)

    has_unhealthy = any(not c.healthy for c in containers)
    return 0 if not has_unhealthy else 2


def _print_table(containers: list[ContainerInfo], threshold_cpu: float, threshold_memory: float) -> None:
    """Print a human-readable table of container statuses."""
    if not containers:
        print("No containers match the given filters.")
        return

    hdr = f"{'NAME':<25} {'STATE':<12} {'CPU%':>7} {'MEM%':>7} {'MEM(MB)':>10} {'HEALTH':<10} {'RESTARTS':>8}"
    sep = "-" * len(hdr)
    print(hdr)
    print(sep)
    for c in containers:
        health_tag = "HEALTHY" if c.healthy else "UNHEALTHY"
        mem_display = f"{c.memory_usage_mb:.1f}/{c.memory_limit_mb:.1f}" if c.memory_limit_mb else "N/A"
        flags = []
        if threshold_cpu > 0 and c.cpu_percent >= threshold_cpu:
            flags.append("HIGH_CPU")
        if threshold_memory > 0 and c.memory_percent >= threshold_memory:
            flags.append("HIGH_MEM")
        flag_str = f" [{', '.join(flags)}]" if flags else ""
        print(f"{c.name:<25} {c.state:<12} {c.cpu_percent:>7.2f} {c.memory_percent:>7.2f} {mem_display:>10} {health_tag:<10} {c.restart_count:>8}{flag_str}")
    print(sep)
    summary = f"Total: {len(containers)} | "
    healthy = sum(1 for c in containers if c.healthy)
    unhealthy = len(containers) - healthy
    summary += f"Healthy: {healthy} | Unhealthy: {unhealthy}"
    print(summary)


def build_parser() -> argparse.ArgumentParser:
    """Build and return the argument parser."""
    parser = argparse.ArgumentParser(
        prog="docker-health-monitor",
        description="Simple Docker Container Health Monitor for DevOps workflows.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --filter label=env.production
  %(prog)s --unhealthy-only --format json
  %(prog)s --cpu-threshold 80 --memory-threshold 90
  %(prog)s --sort-by cpu_percent --reverse
        """,
    )
    parser.add_argument(
        "-f", "--filter",
        metavar="LABEL",
        help='Docker label filter (e.g. "env.production"). Lists all containers when omitted.',
        default=None,
    )
    parser.add_argument(
        "--healthy-only",
        action="store_true",
        help="Show only containers whose health check reports 'healthy'.",
    )
    parser.add_argument(
        "--unhealthy-only",
        action="store_true",
        help="Show only containers with non-healthy or missing health status.",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json"],
        default="table",
        metavar="FMT",
        help="Output format (default: table).",
    )
    parser.add_argument(
        "--sort-by",
        choices=ContainerInfo.FIELDS,
        default=None,
        metavar="FIELD",
        help="Sort output by a container field.",
    )
    parser.add_argument(
        "--reverse",
        action="store_true",
        help="Reverse sort order.",
    )
    parser.add_argument(
        "--cpu-threshold",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Minimum CPU usage % to flag as high (0 = no flagging).",
    )
    parser.add_argument(
        "--memory-threshold",
        type=float,
        default=0.0,
        metavar="PCT",
        help="Minimum memory usage % to flag as high (0 = no flagging).",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug-level logging to stderr.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the Docker Container Health Monitor CLI."""
    parser = build_parser()
    args = parser.parse_args(argv)

    log_level = logging.DEBUG if args.verbose else logging.WARNING
    logging.basicConfig(
        level=log_level,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    logger.debug("Arguments: %s", args)

    try:
        return monitor_containers(
            container_filter=args.filter,
            healthy_only=args.healthy_only,
            unhealthy_only=args.unhealthy_only,
            output_format=args.format,
            sort_by=args.sort_by,
            reverse_sort=args.reverse,
            threshold_cpu=args.cpu_threshold,
            threshold_memory=args.memory_threshold,
        )
    except KeyboardInterrupt:
        print("\nMonitoring interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        logger.error("Unexpected error: %s", exc, exc_info=args.verbose)
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```