"""
CLI entry point for the Hermes Bridge API.

Usage:
    hermes-bridge start           # Start the bridge server
    hermes-bridge start --port 8765 --host 0.0.0.0
    hermes-bridge status          # Check if running
"""

import argparse
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

logger = logging.getLogger("hermes_bridge.cli")


def find_pid_file() -> Path:
    """Return the path to the PID file."""
    return Path.home() / ".hermes" / "bridge.pid"


def find_log_file() -> Path:
    """Return the path to the log file."""
    log_dir = Path.home() / ".hermes" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / "bridge.log"


def cmd_start(args):
    """Start the bridge server."""
    port = args.port
    host = args.host

    if not args.foreground:
        # Check if already running
        pid_file = find_pid_file()
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
                print(f"Bridge already running (PID {pid}) on http://{host}:{port}")
                print("Use --foreground to run in foreground, or stop first.")
                sys.exit(0)
            except (ProcessLookupError, ValueError):
                pid_file.unlink(missing_ok=True)

        # Daemonize: fork and run in background
        pid = os.fork()
        if pid > 0:
            # Parent: print info and exit
            print(f"Hermes Bridge API starting on http://{host}:{port}")
            print(f"PID: {pid}")
            print(f"Logs: {find_log_file()}")
            sys.exit(0)

        # Child: detach and run
        os.setsid()
        # Redirect stdout/stderr to log file
        log_path = find_log_file()
        log_fh = open(log_path, "a")
        os.dup2(log_fh.fileno(), sys.stdout.fileno())
        os.dup2(log_fh.fileno(), sys.stderr.fileno())
        log_fh.close()

        # Write PID
        pid_file = find_pid_file()
        pid_file.write_text(str(os.getpid()))
    else:
        print(f"Hermes Bridge API starting on http://{host}:{port}")

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )

    # Start uvicorn
    import uvicorn

    from .server import app

    # Set Hermes defaults
    if not os.getenv("HERMES_DEFAULT_MODEL"):
        os.environ["HERMES_DEFAULT_MODEL"] = "deepseek-chat"

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=True,
    )


def cmd_stop(args):
    """Stop the bridge server."""
    pid_file = find_pid_file()
    if not pid_file.exists():
        print("Bridge is not running (no PID file found)")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        # Wait for it to stop
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.5)
            except ProcessLookupError:
                break
        pid_file.unlink(missing_ok=True)
        print(f"Bridge stopped (PID {pid})")
    except ProcessLookupError:
        pid_file.unlink(missing_ok=True)
        print("Bridge was not running (stale PID file cleaned up)")
    except Exception as e:
        print(f"Error stopping bridge: {e}")
        sys.exit(1)


def cmd_status(args):
    """Check if the bridge server is running."""
    pid_file = find_pid_file()
    port = args.port

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            print(f"Hermes Bridge API is running:")
            print(f"  PID:     {pid}")
            print(f"  URL:     http://{args.host}:{port}")
            print(f"  Health:  http://{args.host}:{port}/api/v1/health")
            print(f"  Logs:    {find_log_file()}")
            return
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    print("Hermes Bridge API is not running.")
    print(f"Start it with: hermes-bridge start")


def cmd_restart(args):
    """Restart the bridge server."""
    cmd_stop(args)
    time.sleep(1)
    # Make sure foreground mode matches original
    cmd_start(args)


def cmd_pair(args):
    """Show the pairing URL and QR code for the Agentfy app."""
    port = args.port
    host = args.host

    # Get the actual local IP for display (0.0.0.0 means "all interfaces")
    if host == "0.0.0.0":
        from .server import _get_local_ip
        display_host = _get_local_ip()
    else:
        display_host = host

    pairing_url = f"http://{display_host}:{port}"
    api_url = f"http://{host}:{port}/api/v1"

    # Fetch the pairing code from the running bridge
    try:
        import urllib.request, json
        resp = urllib.request.urlopen(f"{api_url}/pairing", timeout=5)
        data = json.loads(resp.read().decode())
        code = data.get("code", "")
    except Exception as e:
        print(f"✗ Could not reach bridge at {api_url}")
        print(f"  Make sure the bridge is running: hermes-bridge start")
        print(f"  Error: {e}")
        sys.exit(1)

    print(f"\n  ┌──────────────────────────────────────────┐")
    print(f"  │                                          │")
    print(f"  │           Hermes Bridge Setup            │")
    print(f"  │                                          │")
    print(f"  └──────────────────────────────────────────┘")
    print()
    print(f"  URL:        {pairing_url}")
    print(f"  Agents:     {data.get('agents', [])}")
    print()
    print(f"  Setup code:")
    print(f"  {code}")
    print()
    print(f"  Scan this QR code with the Agentfy app, or paste the URL above.")
    print()

    # Try to render a QR code in the terminal
    try:
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(pairing_url)
        qr.print_ascii()
        print()
    except ImportError:
        print(f"  Tip: Install 'qrcode' to see a QR code in your terminal:")
        print(f"       pip install qrcode[pil]")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="Hermes Bridge API — REST + SSE bridge for Agentfy",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # start
    start_p = sub.add_parser("start", help="Start the bridge server")
    start_p.add_argument("--port", type=int, default=8765, help="Port to listen on (default: 8765)")
    start_p.add_argument("--host", default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    start_p.add_argument("--foreground", "-f", action="store_true", help="Run in foreground (don't daemonize)")

    # stop
    stop_p = sub.add_parser("stop", help="Stop the bridge server")
    stop_p.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)

    # status
    status_p = sub.add_parser("status", help="Check if the bridge server is running")
    status_p.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    status_p.add_argument("--host", type=str, default="0.0.0.0", help=argparse.SUPPRESS)

    # restart
    restart_p = sub.add_parser("restart", help="Restart the bridge server")
    restart_p.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    restart_p.add_argument("--host", default="0.0.0.0", help=argparse.SUPPRESS)

    # pair
    pair_p = sub.add_parser("pair", help="Show pairing URL + QR code for the Agentfy app")
    pair_p.add_argument("--port", type=int, default=8765, help=argparse.SUPPRESS)
    pair_p.add_argument("--host", type=str, default="0.0.0.0", help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.command == "start":
        cmd_start(args)
    elif args.command == "stop":
        cmd_stop(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "restart":
        cmd_restart(args)
    elif args.command == "pair":
        cmd_pair(args)


if __name__ == "__main__":
    main()
