"""Prepare or explicitly run a temporary HTTPS tunnel for the app on port 8000.

--check makes only a loopback health request. --download only fetches a pinned
official runtime. --serve creates external access and must be authorized first.
The model on port 8081 is never a tunnel target. See docs/HTTPS_DEMO.md.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import tempfile
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener, urlopen


ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".local" / "cloudflared"
VERSION = "2026.10.0"
ASSET = "cloudflared-windows-amd64.exe"
DOWNLOAD_URL = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{ASSET}"
RELEASE_URL = f"https://github.com/cloudflare/cloudflared/releases/tag/{VERSION}"
SHA256 = "86aee4017b26625cee8484c113558f48effa4cd47f7aa05fcf425604e5d2b23c"
EXECUTABLE = CACHE / VERSION / ASSET
TARGET = "http://127.0.0.1:8000"
MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def require_platform() -> None:
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("This pinned runtime is for Windows x64 only.")


def verified_runtime() -> bool:
    return EXECUTABLE.is_file() and digest(EXECUTABLE) == SHA256


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def local_health() -> dict:
    # A loopback check must not be redirected through an environment proxy.
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open(TARGET + "/api/health", timeout=3) as response:
        raw = response.read(64 * 1024 + 1)
        if response.status != 200 or len(raw) > 64 * 1024:
            raise ValueError("Unexpected local app health response.")
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get("status") != "ok" or value.get("api_version") != 1:
        raise ValueError("Port 8000 is not the expected Astana Walks API v1.")
    return value


def download() -> None:
    require_platform()
    if verified_runtime():
        print(f"Verified existing cloudflared {VERSION}; no tunnel started.")
        return
    EXECUTABLE.parent.mkdir(parents=True, exist_ok=True)
    temporary = EXECUTABLE.with_suffix(".exe.part")
    try:
        request = Request(DOWNLOAD_URL, headers={"User-Agent": "AstanaWalksHttpsSetup/1.0"})
        received = 0
        with urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while block := response.read(1024 * 1024):
                received += len(block)
                if received > MAX_DOWNLOAD_BYTES:
                    raise ValueError("Runtime download exceeds the size limit.")
                output.write(block)
        if digest(temporary) != SHA256:
            raise ValueError("SHA-256 mismatch; downloaded executable will not be used.")
        temporary.replace(EXECUTABLE)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Verified cloudflared {VERSION}: {received} bytes. No tunnel started.")


def child_environment(directory: Path) -> dict[str, str]:
    # Do not inherit API keys, TUNNEL_* flags, named-tunnel tokens, logging paths,
    # proxy overrides or the user's existing cloudflared profile.
    allowed = {"SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE", "NUMBER_OF_PROCESSORS"}
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    env.update(USERPROFILE=str(directory), XDG_CONFIG_HOME=str(directory))
    return env


def command(config: Path, allowed_mail: list[str]) -> list[str]:
    args = [str(EXECUTABLE), "tunnel", "--config", str(config), "--no-autoupdate",
            "--url", TARGET, "--metrics", "127.0.0.1:0", "--loglevel", "info", "--grace-period", "3s"]
    for address in allowed_mail:
        args.extend(["--allowed-mail", address])
    return args


def serve(allowed_mail: list[str]) -> int:
    require_platform()
    if not verified_runtime():
        raise ValueError("Pinned runtime is missing or has a different SHA-256. Run --download first.")
    health = local_health()
    print(f"External HTTPS access will target only {TARGET} ({health.get('places_count', '?')} POI).", flush=True)
    print("Access: email one-time PIN." if allowed_mail else "Access: anyone who has the generated URL.", flush=True)
    print("Keep the app and model running separately. Ctrl+C stops only this tunnel.", flush=True)
    with tempfile.TemporaryDirectory(prefix="session-", dir=CACHE) as temporary:
        directory = Path(temporary)
        config = directory / "config.yml"
        config.write_text("{}\n", encoding="utf-8")
        process = subprocess.Popen(command(config, allowed_mail), cwd=directory, env=child_environment(directory),
                                   shell=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            return process.wait()
        except KeyboardInterrupt:
            return 0
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def email(value: str) -> str:
    if len(value) > 254 or "*" in value or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value):
        raise argparse.ArgumentTypeError("Use one explicit email address; repeat --allowed-mail for another.")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--check", action="store_true", help="Verify runtime and app health; no external connection or tunnel")
    operation.add_argument("--download", action="store_true", help="Download the pinned official runtime; do not execute it")
    operation.add_argument("--serve", action="store_true", help="Explicitly create a temporary external HTTPS tunnel after authorization")
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--allowed-mail", action="append", default=[], type=email, help="Protect access with an email PIN; may be repeated")
    access.add_argument("--public", action="store_true", help="Explicitly allow anyone with the generated URL")
    args = parser.parse_args(argv)
    if args.serve and not (args.allowed_mail or args.public):
        parser.error("--serve requires --allowed-mail ADDRESS or explicit --public.")
    if not args.serve and (args.allowed_mail or args.public):
        parser.error("Access choices apply only to --serve.")
    try:
        if args.download:
            download()
        elif args.serve:
            return serve(args.allowed_mail)
        else:
            require_platform()
            runtime_ok = verified_runtime()
            print(f"Pinned runtime: cloudflared {VERSION}; SHA-256 {'verified' if runtime_ok else 'missing or different'}.")
            health = local_health()
            print(f"Local API: ok; {health.get('places_count', '?')} POI; LLM configured: {health.get('llm_configured', False)}.")
            print(f"Fixed target: {TARGET}; no tunnel started; port 8081 is never exposed.")
            return 0 if runtime_ok else 1
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"HTTPS demo: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
