#!/usr/bin/env python3
"""
server_resource_monitor.py
==========================
Monitor CPU, memory, swap, disk, network, load average, temperatures and the
top processes; log warnings/criticals when a threshold is crossed, and export
samples to CSV/JSON for later charting.

  * one-shot or continuous sampling with --interval / --count / --duration
  * per-metric warning and critical thresholds
  * "sustained breach" logic: alerts only after N consecutive bad samples,
    so a single spike does not page anybody
  * optional webhook / e-mail alert on state change
  * top-N processes by CPU and RSS when a threshold trips

Examples
--------
    python server_resource_monitor.py
    python server_resource_monitor.py --interval 30 --count 10 --csv metrics.csv
    python server_resource_monitor.py --cpu-warn 70 --cpu-crit 90 --disk-crit 95 --loop
    python server_resource_monitor.py --loop --interval 60 --webhook $SLACK_URL --breaches 3
    python server_resource_monitor.py --json snapshot.json --top 10
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import platform
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import psutil
except ImportError:  # pragma: no cover
    sys.exit("Missing dependency: psutil.  Install with:  pip install psutil")

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

LOG = logging.getLogger("resource_monitor")

LEVEL_OK = "OK"
LEVEL_WARN = "WARNING"
LEVEL_CRIT = "CRITICAL"


def configure_logging(verbose: bool = False, log_file: str | None = None) -> None:
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            print("WARNING: cannot open log file %s: %s" % (log_file, exc), file=sys.stderr)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024.0:
            return "%.1f %s" % (num_bytes, unit)
        num_bytes /= 1024.0
    return "%.1f PB" % num_bytes


# --------------------------------------------------------------------------- #
# Collection
# --------------------------------------------------------------------------- #
def collect_cpu(sample_seconds: float) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        data["cpu_percent"] = psutil.cpu_percent(interval=sample_seconds)
        data["cpu_count_logical"] = psutil.cpu_count(logical=True)
        data["cpu_count_physical"] = psutil.cpu_count(logical=False)
        per_core = psutil.cpu_percent(interval=None, percpu=True)
        data["cpu_per_core"] = per_core
        data["cpu_core_max"] = max(per_core) if per_core else None
    except Exception as exc:
        LOG.warning("CPU metrics unavailable: %s", exc)

    try:
        times = psutil.cpu_times_percent(interval=None)
        data["cpu_user"] = getattr(times, "user", None)
        data["cpu_system"] = getattr(times, "system", None)
        data["cpu_iowait"] = getattr(times, "iowait", None)
        data["cpu_idle"] = getattr(times, "idle", None)
    except Exception as exc:
        LOG.debug("cpu_times_percent unavailable: %s", exc)

    try:
        frequency = psutil.cpu_freq()
        if frequency:
            data["cpu_freq_mhz"] = round(frequency.current, 1)
    except Exception as exc:
        LOG.debug("cpu_freq unavailable: %s", exc)

    try:
        if hasattr(os, "getloadavg"):
            one, five, fifteen = os.getloadavg()
            cores = data.get("cpu_count_logical") or 1
            data["load_1m"] = round(one, 2)
            data["load_5m"] = round(five, 2)
            data["load_15m"] = round(fifteen, 2)
            data["load_1m_per_core"] = round(one / cores, 2)
    except Exception as exc:
        LOG.debug("Load average unavailable: %s", exc)

    return data


def collect_memory() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        memory = psutil.virtual_memory()
        data.update({
            "ram_percent": memory.percent,
            "ram_total_bytes": memory.total,
            "ram_used_bytes": memory.used,
            "ram_available_bytes": memory.available,
        })
    except Exception as exc:
        LOG.warning("Memory metrics unavailable: %s", exc)

    try:
        swap = psutil.swap_memory()
        data.update({
            "swap_percent": swap.percent,
            "swap_total_bytes": swap.total,
            "swap_used_bytes": swap.used,
        })
    except Exception as exc:
        LOG.debug("Swap metrics unavailable: %s", exc)

    return data


def collect_disks(paths: Sequence[str] | None) -> List[Dict[str, Any]]:
    disks: List[Dict[str, Any]] = []

    if paths:
        mountpoints = [(p, p, "") for p in paths]
    else:
        try:
            mountpoints = [
                (part.mountpoint, part.device, part.fstype)
                for part in psutil.disk_partitions(all=False)
                if part.fstype and "cdrom" not in (part.opts or "")
            ]
        except Exception as exc:
            LOG.warning("Cannot enumerate partitions: %s", exc)
            mountpoints = [(os.path.abspath(os.sep), "root", "")]

    for mountpoint, device, fstype in mountpoints:
        try:
            usage = psutil.disk_usage(mountpoint)
            disks.append({
                "mountpoint": mountpoint,
                "device": device,
                "fstype": fstype,
                "percent": usage.percent,
                "total_bytes": usage.total,
                "used_bytes": usage.used,
                "free_bytes": usage.free,
            })
        except (PermissionError, OSError) as exc:
            LOG.debug("Cannot read usage for %s: %s", mountpoint, exc)

    return disks


def collect_io_and_network(previous: Optional[Dict[str, Any]],
                           elapsed: float) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        net = psutil.net_io_counters()
        data["net_bytes_sent"] = net.bytes_sent
        data["net_bytes_recv"] = net.bytes_recv
        data["net_errin"] = net.errin
        data["net_errout"] = net.errout
        data["net_dropin"] = net.dropin
        data["net_dropout"] = net.dropout
        if previous and elapsed > 0:
            data["net_sent_rate_bps"] = max(
                0, (net.bytes_sent - previous.get("net_bytes_sent", net.bytes_sent)) / elapsed)
            data["net_recv_rate_bps"] = max(
                0, (net.bytes_recv - previous.get("net_bytes_recv", net.bytes_recv)) / elapsed)
    except Exception as exc:
        LOG.debug("Network counters unavailable: %s", exc)

    try:
        disk_io = psutil.disk_io_counters()
        if disk_io:
            data["disk_read_bytes"] = disk_io.read_bytes
            data["disk_write_bytes"] = disk_io.write_bytes
            if previous and elapsed > 0:
                data["disk_read_rate_bps"] = max(
                    0, (disk_io.read_bytes
                        - previous.get("disk_read_bytes", disk_io.read_bytes)) / elapsed)
                data["disk_write_rate_bps"] = max(
                    0, (disk_io.write_bytes
                        - previous.get("disk_write_bytes", disk_io.write_bytes)) / elapsed)
    except Exception as exc:
        LOG.debug("Disk I/O counters unavailable: %s", exc)

    try:
        connections = psutil.net_connections(kind="inet")
        data["connections_total"] = len(connections)
        data["connections_established"] = sum(
            1 for c in connections if c.status == psutil.CONN_ESTABLISHED)
    except (psutil.AccessDenied, PermissionError) as exc:
        LOG.debug("Connection counters need elevated rights: %s", exc)
    except Exception as exc:
        LOG.debug("Connection counters unavailable: %s", exc)

    return data


def collect_misc() -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    try:
        boot = datetime.fromtimestamp(psutil.boot_time())
        data["boot_time"] = boot.isoformat(timespec="seconds")
        data["uptime_hours"] = round((datetime.now() - boot).total_seconds() / 3600, 1)
    except Exception as exc:
        LOG.debug("Boot time unavailable: %s", exc)

    try:
        data["process_count"] = len(psutil.pids())
    except Exception as exc:
        LOG.debug("Process count unavailable: %s", exc)

    try:
        sensors = getattr(psutil, "sensors_temperatures", None)
        if sensors:
            readings = sensors()
            temperatures = [entry.current for entries in readings.values()
                            for entry in entries if entry.current]
            if temperatures:
                data["temperature_max_c"] = round(max(temperatures), 1)
    except Exception as exc:
        LOG.debug("Temperature sensors unavailable: %s", exc)

    try:
        battery = getattr(psutil, "sensors_battery", None)
        if battery:
            info = battery()
            if info:
                data["battery_percent"] = round(info.percent, 1)
                data["battery_plugged"] = info.power_plugged
    except Exception as exc:
        LOG.debug("Battery sensor unavailable: %s", exc)

    return data


def top_processes(count: int, sort_by: str = "cpu",
                  sample_seconds: float = 0.4) -> List[Dict[str, Any]]:
    """Return the heaviest processes by CPU or RSS.

    psutil's per-process cpu_percent() is measured *between two calls*: the
    first one always returns 0.0. So for a CPU ranking we prime every process,
    wait, and only then read the real figures.
    """
    handles: List[psutil.Process] = []
    for process in psutil.process_iter():
        try:
            if sort_by == "cpu":
                process.cpu_percent(None)  # prime the counter
            handles.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if sort_by == "cpu" and sample_seconds > 0:
        time.sleep(sample_seconds)

    cores = psutil.cpu_count(logical=True) or 1
    processes: List[Dict[str, Any]] = []

    for process in handles:
        try:
            with process.oneshot():
                memory_info = process.memory_info()
                # Normalise to a share of the whole machine, as top -i does.
                cpu = process.cpu_percent(None) / cores if sort_by == "cpu" else 0.0
                processes.append({
                    "pid": process.pid,
                    "name": (process.name() or "?")[:32],
                    "user": (process.username() or "?")[:20],
                    "cpu_percent": round(cpu, 1),
                    "memory_percent": round(process.memory_percent(), 1),
                    "rss_bytes": getattr(memory_info, "rss", 0),
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
        except Exception as exc:
            LOG.debug("Skipping a process: %s", exc)

    key = "cpu_percent" if sort_by == "cpu" else "rss_bytes"
    processes.sort(key=lambda p: p.get(key, 0), reverse=True)
    return processes[:count]


# --------------------------------------------------------------------------- #
# Threshold evaluation
# --------------------------------------------------------------------------- #
def evaluate(sample: Dict[str, Any], disks: List[Dict[str, Any]],
             args: argparse.Namespace) -> List[Dict[str, Any]]:
    """Compare the sample against the thresholds and return the breaches."""
    breaches: List[Dict[str, Any]] = []

    def check(metric: str, value: Optional[float], warn: Optional[float],
              crit: Optional[float], unit: str = "%") -> None:
        if value is None:
            return
        if crit is not None and value >= crit:
            breaches.append({"metric": metric, "value": value, "threshold": crit,
                             "level": LEVEL_CRIT, "unit": unit})
        elif warn is not None and value >= warn:
            breaches.append({"metric": metric, "value": value, "threshold": warn,
                             "level": LEVEL_WARN, "unit": unit})

    check("cpu", sample.get("cpu_percent"), args.cpu_warn, args.cpu_crit)
    check("ram", sample.get("ram_percent"), args.ram_warn, args.ram_crit)
    check("swap", sample.get("swap_percent"), args.swap_warn, args.swap_crit)
    check("load_per_core", sample.get("load_1m_per_core"),
          args.load_warn, args.load_crit, unit="")
    check("temperature", sample.get("temperature_max_c"),
          args.temp_warn, args.temp_crit, unit="C")

    for disk in disks:
        check("disk:%s" % disk["mountpoint"], disk["percent"],
              args.disk_warn, args.disk_crit)
        if args.disk_free_min_gb:
            free_gb = disk["free_bytes"] / (1024 ** 3)
            if free_gb < args.disk_free_min_gb:
                breaches.append({
                    "metric": "disk_free:%s" % disk["mountpoint"],
                    "value": round(free_gb, 1), "threshold": args.disk_free_min_gb,
                    "level": LEVEL_CRIT, "unit": "GB free (below minimum)",
                })

    return breaches


def worst_level(breaches: Sequence[Dict[str, Any]]) -> str:
    if any(b["level"] == LEVEL_CRIT for b in breaches):
        return LEVEL_CRIT
    if any(b["level"] == LEVEL_WARN for b in breaches):
        return LEVEL_WARN
    return LEVEL_OK


# --------------------------------------------------------------------------- #
# Output / alerting
# --------------------------------------------------------------------------- #
def log_sample(sample: Dict[str, Any], disks: List[Dict[str, Any]],
               breaches: List[Dict[str, Any]], args: argparse.Namespace) -> None:
    LOG.info("CPU %5.1f%%  RAM %5.1f%% (%s/%s)  SWAP %5.1f%%  procs %s%s",
             sample.get("cpu_percent") or 0.0,
             sample.get("ram_percent") or 0.0,
             human_size(sample.get("ram_used_bytes") or 0),
             human_size(sample.get("ram_total_bytes") or 0),
             sample.get("swap_percent") or 0.0,
             sample.get("process_count", "?"),
             "  load %.2f/core" % sample["load_1m_per_core"]
             if sample.get("load_1m_per_core") is not None else "")

    for disk in disks:
        level = logging.INFO
        if args.disk_crit and disk["percent"] >= args.disk_crit:
            level = logging.ERROR
        elif args.disk_warn and disk["percent"] >= args.disk_warn:
            level = logging.WARNING
        LOG.log(level, "DISK %-26s %5.1f%%  %s free of %s",
                disk["mountpoint"][:26], disk["percent"],
                human_size(disk["free_bytes"]), human_size(disk["total_bytes"]))

    if sample.get("net_recv_rate_bps") is not None:
        LOG.info("NET  in %s/s  out %s/s   connections %s",
                 human_size(sample["net_recv_rate_bps"]),
                 human_size(sample.get("net_sent_rate_bps", 0)),
                 sample.get("connections_established", "?"))

    for breach in breaches:
        LOG.log(logging.CRITICAL if breach["level"] == LEVEL_CRIT else logging.WARNING,
                "%s: %s at %.1f%s (threshold %.1f)",
                breach["level"], breach["metric"], breach["value"],
                breach["unit"], breach["threshold"])


def append_csv(path: Path, sample: Dict[str, Any]) -> None:
    flat = {k: v for k, v in sample.items() if not isinstance(v, (list, dict))}
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.is_file()
    try:
        with path.open("a", newline="", encoding="utf-8-sig") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(flat))
            if not exists:
                writer.writeheader()
            writer.writerow(flat)
    except OSError as exc:
        LOG.error("Cannot append to %s: %s", path, exc)


def send_webhook(url: str, hostname: str, level: str,
                 breaches: Sequence[Dict[str, Any]], sample: Dict[str, Any]) -> None:
    if not HAS_REQUESTS:
        LOG.warning("Webhook alerts need the requests package - skipped")
        return

    lines = ["*%s on %s*" % (level, hostname)]
    for breach in breaches:
        lines.append("- %s: %.1f%s (threshold %.1f)"
                     % (breach["metric"], breach["value"], breach["unit"],
                        breach["threshold"]))
    lines.append("CPU %.1f%% | RAM %.1f%% | %s"
                 % (sample.get("cpu_percent") or 0.0,
                    sample.get("ram_percent") or 0.0,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S")))

    try:
        response = requests.post(url, json={"text": "\n".join(lines)}, timeout=15)
        response.raise_for_status()
        LOG.info("Alert delivered to the webhook (HTTP %d)", response.status_code)
    except Exception as exc:
        LOG.error("Could not deliver the webhook alert: %s", exc)


# --------------------------------------------------------------------------- #
# Sampling loop
# --------------------------------------------------------------------------- #
def take_sample(args: argparse.Namespace, previous: Optional[Dict[str, Any]],
                elapsed: float) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    sample: Dict[str, Any] = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
    }
    sample.update(collect_cpu(args.cpu_sample))
    sample.update(collect_memory())
    sample.update(collect_io_and_network(previous, elapsed))
    sample.update(collect_misc())

    disks = collect_disks(args.disk_path)
    for disk in disks:
        key = "disk_percent:%s" % disk["mountpoint"]
        sample[key] = disk["percent"]

    return sample, disks


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Monitor CPU, RAM and disk usage and log threshold warnings.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--interval", type=float, default=5.0,
                        help="Seconds between samples when looping")
    parser.add_argument("--count", type=int, help="Stop after N samples")
    parser.add_argument("--duration", type=float, help="Stop after N seconds")
    parser.add_argument("--loop", action="store_true", help="Sample continuously")
    parser.add_argument("--cpu-sample", type=float, default=1.0,
                        help="cpu_percent measurement window (seconds)")
    parser.add_argument("--cpu-warn", type=float, default=80.0, help="CPU warning threshold %%")
    parser.add_argument("--cpu-crit", type=float, default=95.0, help="CPU critical threshold %%")
    parser.add_argument("--ram-warn", type=float, default=80.0, help="RAM warning threshold %%")
    parser.add_argument("--ram-crit", type=float, default=93.0, help="RAM critical threshold %%")
    parser.add_argument("--swap-warn", type=float, default=50.0, help="Swap warning threshold %%")
    parser.add_argument("--swap-crit", type=float, default=80.0, help="Swap critical threshold %%")
    parser.add_argument("--disk-warn", type=float, default=80.0, help="Disk warning threshold %%")
    parser.add_argument("--disk-crit", type=float, default=92.0, help="Disk critical threshold %%")
    parser.add_argument("--disk-free-min-gb", type=float,
                        help="Alert when free space falls below this many GB")
    parser.add_argument("--disk-path", action="append",
                        help="Only monitor this mountpoint (repeatable)")
    parser.add_argument("--load-warn", type=float, default=1.5,
                        help="Load average per core warning threshold")
    parser.add_argument("--load-crit", type=float, default=3.0,
                        help="Load average per core critical threshold")
    parser.add_argument("--temp-warn", type=float, default=75.0,
                        help="Temperature warning threshold (C)")
    parser.add_argument("--temp-crit", type=float, default=88.0,
                        help="Temperature critical threshold (C)")
    parser.add_argument("--breaches", type=int, default=1,
                        help="Consecutive bad samples required before alerting")
    parser.add_argument("--top", type=int, default=5,
                        help="Show the top N processes when a threshold trips")
    parser.add_argument("--always-top", action="store_true",
                        help="Always show the top processes")
    parser.add_argument("--csv", help="Append every sample to this CSV file")
    parser.add_argument("--json", help="Write the last sample to this JSON file")
    parser.add_argument("--webhook", help="POST alerts to this webhook URL")
    parser.add_argument("--log-file", help="Write logs to this file as well")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose, args.log_file)

    try:
        LOG.info("=" * 66)
        LOG.info("Resource monitor on %s (%s %s, Python %s)",
                 socket.gethostname(), platform.system(), platform.release(),
                 platform.python_version())
        LOG.info("CPU cores: %s logical / %s physical",
                 psutil.cpu_count(logical=True), psutil.cpu_count(logical=False))
        LOG.info("Thresholds: cpu %s/%s  ram %s/%s  disk %s/%s (warn/crit)",
                 args.cpu_warn, args.cpu_crit, args.ram_warn, args.ram_crit,
                 args.disk_warn, args.disk_crit)
        LOG.info("=" * 66)

        # Prime the CPU counters so the first reading is meaningful.
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(interval=None, percpu=True)

        previous: Optional[Dict[str, Any]] = None
        last_time = time.time()
        samples = 0
        started = time.time()
        consecutive = 0
        last_alert_level = LEVEL_OK
        exit_code = 0
        last_sample: Dict[str, Any] = {}

        while True:
            now = time.time()
            sample, disks = take_sample(args, previous, now - last_time)
            previous = sample
            last_time = now
            samples += 1
            last_sample = sample

            breaches = evaluate(sample, disks, args)
            level = worst_level(breaches)

            LOG.info("-" * 66)
            log_sample(sample, disks, breaches, args)

            if breaches:
                consecutive += 1
            else:
                if last_alert_level != LEVEL_OK:
                    LOG.info("RECOVERED - every metric is back within its thresholds")
                    if args.webhook:
                        send_webhook(args.webhook, sample["hostname"], "RECOVERED",
                                     [], sample)
                consecutive = 0
                last_alert_level = LEVEL_OK

            if breaches and consecutive >= args.breaches:
                if level != last_alert_level:
                    if args.webhook:
                        send_webhook(args.webhook, sample["hostname"], level,
                                     breaches, sample)
                    last_alert_level = level
                exit_code = 2 if level == LEVEL_CRIT else max(exit_code, 1)

            if args.always_top or (breaches and args.top):
                LOG.info("Top %d process(es) by CPU:", args.top)
                for process in top_processes(args.top, "cpu"):
                    LOG.info("  %6s %-30s %-16s cpu %5.1f%%  rss %9s",
                             process["pid"], process["name"], process["user"],
                             process["cpu_percent"], human_size(process["rss_bytes"]))
                LOG.info("Top %d process(es) by memory:", args.top)
                for process in top_processes(args.top, "rss"):
                    LOG.info("  %6s %-30s %-16s rss %9s  (%4.1f%%)",
                             process["pid"], process["name"], process["user"],
                             human_size(process["rss_bytes"]), process["memory_percent"])

            if args.csv:
                append_csv(Path(args.csv).expanduser(), sample)

            if not args.loop:
                break
            if args.count and samples >= args.count:
                LOG.info("Reached --count (%d) - stopping", args.count)
                break
            if args.duration and (time.time() - started) >= args.duration:
                LOG.info("Reached --duration (%.0fs) - stopping", args.duration)
                break

            time.sleep(max(args.interval - args.cpu_sample, 0.1))

        if args.json:
            output = Path(args.json).expanduser()
            output.parent.mkdir(parents=True, exist_ok=True)
            with output.open("w", encoding="utf-8") as fh:
                json.dump({"sample": last_sample,
                           "disks": collect_disks(args.disk_path),
                           "top_cpu": top_processes(args.top, "cpu"),
                           "top_memory": top_processes(args.top, "rss")},
                          fh, indent=2, default=str)
            LOG.info("Snapshot written to %s", output)

        LOG.info("Monitoring finished after %d sample(s)", samples)
        return exit_code

    except KeyboardInterrupt:
        LOG.warning("Interrupted by user")
        return 130
    except Exception as exc:
        LOG.error("Monitoring failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
