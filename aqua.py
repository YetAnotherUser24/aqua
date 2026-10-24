#!/usr/bin/env python3
"""
AquaControl Unified Monorepo CLI & Launcher.

Quick commands:
    ./aqua.py dev                     # Launch local dev-stack (DB, Broker, Gateway, UI)
    ./aqua.py virtual                 # Launch virtual nodes bridge (4 ESP32 simulated roles)
    ./aqua.py test                    # Run all test suites (gateway pytest + virtual nodes)
    ./aqua.py test --gateway          # Run only gateway tests
    ./aqua.py build <node> [--virtual]# Build firmware (aerador, bomba, mezclador, sensor-od)
    ./aqua.py status                  # Display workspace health and node configs
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATION_DIR = ROOT / "aqua-station"
GATEWAY_DIR = STATION_DIR / "gateway"
TOOLS_DIR = STATION_DIR / "tools"
FIRMWARE_CHECK_DIR = STATION_DIR / "firmware" / "virtual_node"

NODE_ENVS = {
    "aerador": {
        "dir": ROOT / "nodo-aerador",
        "real": "esp32-s3",
        "virtual": "esp32-s3-virtual",
    },
    "bomba": {
        "dir": ROOT / "nodo-bomba",
        "real": "esp32dev",
        "virtual": "esp32dev-virtual",
    },
    "mezclador": {
        "dir": ROOT / "nodo-mezclador",
        "real": "esp32-s3",
        "virtual": "esp32-s3-virtual",
    },
    "sensor-od": {
        "dir": ROOT / "nodo-sensor-od",
        "real": "lora32",
        "virtual": "virtual-s3",
    },
}


def find_venv_python() -> Path:
    candidates = [
        GATEWAY_DIR / ".venv" / "bin" / "python3",
        ROOT / ".venv-gateway" / "bin" / "python3",
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return p
    return Path(sys.executable)


def find_venv_pytest() -> Path:
    candidates = [
        GATEWAY_DIR / ".venv" / "bin" / "pytest",
        ROOT / ".venv-gateway" / "bin" / "pytest",
    ]
    for p in candidates:
        if p.exists() and os.access(p, os.X_OK):
            return p
    return Path("pytest")


def cmd_dev(args: argparse.Namespace) -> int:
    """Launch the all-in-one local development stack."""
    dev_stack_script = TOOLS_DIR / "dev-stack.py"
    if not dev_stack_script.exists():
        sys.exit(f"Error: {dev_stack_script} not found.")

    py = find_venv_python()
    cmd = [str(py), str(dev_stack_script)]
    if args.seconds:
        cmd.extend(["--seconds", str(args.seconds)])
    print(f"[Aqua] Starting local development stack with {py.name}...")
    return subprocess.call(cmd)


def cmd_virtual(args: argparse.Namespace) -> int:
    """Launch virtual nodes bridge."""
    vnodes_script = TOOLS_DIR / "virtual-nodes.py"
    if not vnodes_script.exists():
        sys.exit(f"Error: {vnodes_script} not found.")

    py = find_venv_python()
    cmd = [str(py), str(vnodes_script), "--hz", str(args.hz), "--host", args.host, "--port", str(args.port)]
    print(f"[Aqua] Starting virtual node bridge at {args.host}:{args.port}...")
    return subprocess.call(cmd)


def cmd_build(args: argparse.Namespace) -> int:
    """Build firmware for one or all nodes via PlatformIO."""
    node_name = args.node.lower() if args.node else "all"
    is_virtual = args.virtual

    targets = [node_name] if node_name != "all" else list(NODE_ENVS.keys())

    for target in targets:
        if target not in NODE_ENVS:
            print(f"[Aqua] Unknown node '{target}'. Valid options: {list(NODE_ENVS.keys())} or 'all'")
            return 1

        info = NODE_ENVS[target]
        env_name = info["virtual"] if is_virtual else info["real"]
        cmd = ["pio", "run", "-d", str(info["dir"]), "-e", env_name]
        print(f"\n[Aqua] Building '{target}' (Env: {env_name}) in {info['dir'].name}...")
        rc = subprocess.call(cmd)
        if rc != 0:
            print(f"[Aqua] Build failed for {target} with exit code {rc}")
            return rc

    print("\n[Aqua] All requested builds succeeded!")
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    """Run test suites across gateway and firmware."""
    run_all = not (args.gateway or args.nodes)
    failed = False

    if args.gateway or run_all:
        pytest_bin = find_venv_pytest()
        print(f"\n[Aqua] Running Gateway Pytest Suite ({pytest_bin})...")
        rc = subprocess.call([str(pytest_bin)], cwd=str(GATEWAY_DIR))
        if rc != 0:
            failed = True

    if args.nodes or run_all:
        py = find_venv_python()
        check_script = FIRMWARE_CHECK_DIR / "check_closed_loop.py"
        if check_script.exists():
            print(f"\n[Aqua] Running Virtual Node Closed-Loop Validation...")
            rc = subprocess.call([str(py), str(check_script)], cwd=str(FIRMWARE_CHECK_DIR))
            if rc != 0:
                print(f"[Aqua] check_closed_loop.py returned {rc}")
                failed = True

    if failed:
        print("\n[Aqua] Tests completed with FAILURES.")
        return 1

    print("\n[Aqua] All tests passed cleanly!")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    """Show workspace status and node mapping."""
    print("==================================================================")
    print("  AquaControl Workspace Status")
    print("==================================================================")
    print("Node Environments & Directories:")
    for name, info in NODE_ENVS.items():
        print(f"  - {name:<12}: Real: [{info['real']:<15}] Virtual: [{info['virtual']:<16}] ({info['dir'].name})")

    print("\nRepository Git Status:")
    subprocess.call(["git", "status", "-s"], cwd=str(ROOT))
    print("==================================================================")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="AquaControl Unified CLI & Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # dev
    p_dev = subparsers.add_parser("dev", help="Start local development stack (DB, Gateway, Web, Broker)")
    p_dev.add_argument("--seconds", type=int, help="Optional duration in seconds before auto-exit")
    p_dev.set_defaults(func=cmd_dev)

    # virtual
    p_virt = subparsers.add_parser("virtual", help="Launch virtual nodes bridge to MQTT broker")
    p_virt.add_argument("--hz", type=int, default=5, help="Simulation tick frequency (default: 5 Hz)")
    p_virt.add_argument("--host", default="127.0.0.1", help="MQTT broker host (default: 127.0.0.1)")
    p_virt.add_argument("--port", type=int, default=1883, help="MQTT broker port (default: 1883)")
    p_virt.set_defaults(func=cmd_virtual)

    # build
    p_build = subparsers.add_parser("build", help="Build firmware via PlatformIO")
    p_build.add_argument("node", nargs="?", default="all", help="Node to build (aerador, bomba, mezclador, sensor-od, or all)")
    p_build.add_argument("--virtual", action="store_true", help="Build virtual environment (-D..._VIRTUAL=1)")
    p_build.set_defaults(func=cmd_build)

    # test
    p_test = subparsers.add_parser("test", help="Run test suites")
    p_test.add_argument("--gateway", action="store_true", help="Run gateway pytest suite only")
    p_test.add_argument("--nodes", action="store_true", help="Run firmware / virtual node checks only")
    p_test.set_defaults(func=cmd_test)

    # status
    p_status = subparsers.add_parser("status", help="Show workspace status and node mappings")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
