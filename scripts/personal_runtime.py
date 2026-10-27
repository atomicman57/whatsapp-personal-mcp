#!/usr/bin/env python3
"""Start the local bridge/MCP with its secret loaded from the encrypted store."""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("bridge", "pair", "mcp", "http", "health"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    # Capture the owning helper's stdout in memory; never print the secret or
    # place it in argv, a launchd plist, a Grokbot configuration or a log file.
    result = subprocess.run([config["credential_helper"], "get", config["token_credential"]],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    if result.returncode or len(result.stdout.strip()) < 32:
        raise SystemExit("Bridge credential unavailable from encrypted store")
    token = result.stdout.strip()
    env = {**os.environ, "WHATSAPP_BRIDGE_TOKEN": token,
        "WHATSAPP_STORE_DIR": config["store_dir"],
        "WHATSAPP_BRIDGE_URL": f"http://127.0.0.1:{config['port']}/api",
        "WHATSAPP_BRIDGE_INSTANCE_ID": config["instance_id"],
        "WHATSAPP_SEND_ENABLED": str(config.get("send_enabled", False)).lower(),
        "WHATSAPP_MUTATIONS_ENABLED": str(config.get("mutations_enabled", False)).lower(),
        "WHATSAPP_DO_NOT_CONTACT": ",".join(config.get("do_not_contact", [])),
        "WHATSAPP_SEND_ALLOWLIST": ",".join(config.get("send_allowlist", [])),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + os.environ.get("PATH", "")}
    if args.mode == "health":
        request = urllib.request.Request(env["WHATSAPP_BRIDGE_URL"] + "/health",
            headers={"Authorization": "Bearer " + token})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                health = json.load(response)
            print(json.dumps({key: health.get(key) for key in (
                "status", "connected", "logged_in", "instance_id", "reads_may_be_stale", "coverage", "build_version")}))
        except (urllib.error.URLError, ValueError):
            raise SystemExit("Bridge unavailable or not authorized")
        return
    if args.mode in ("mcp", "http"):
        command = [config["python"], str(Path(config["repo"]) / "whatsapp-mcp-server/main.py")]
        if args.mode == "http":
            command += ["--http", "--port", str(config["mcp_port"])]
    else:
        command = [config["bridge"], "--store-dir", config["store_dir"],
            "--port", str(config["port"]), "--log-level", "WARN"]
        if args.mode == "bridge":
            command.append("--no-pair")
        elif not sys.stdin.isatty():
            raise SystemExit("Pairing requires an interactive owner terminal; pairing codes must not be logged")
    os.execve(command[0], command, env)


if __name__ == "__main__":
    main()
