"""Install a pinned, checksum-verified local LLM runtime; no cloud key needed.

python scripts/setup_local_llm.py --download
python scripts/setup_local_llm.py --serve

Windows x64 CPU reference setup. On Linux/macOS use a llama.cpp server with the
same model and point LLM_BASE_URL at its loopback /v1 endpoint.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import sys
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / ".local" / "llm"
RELEASE = "b11739"
RUNTIME_NAME = f"llama-{RELEASE}-bin-win-cpu-x64.zip"
RUNTIME_URL = f"https://github.com/ggml-org/llama.cpp/releases/download/{RELEASE}/{RUNTIME_NAME}"
RUNTIME_SHA256 = "b1e0a14abdb4665c1b821b377ba063b221cec62882c373065cf8fb672988d3ff"
MODEL_REPO = "Qwen/Qwen3-1.7B-GGUF"
MODEL_REVISION = "90862c4b9d2787eaed51d12237eafdfe7c5f6077"
MODEL_NAME = "Qwen3-1.7B-Q8_0.gguf"
HEADERS = {"User-Agent": "AstanaWalksLocalSetup/1.0"}

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def download(url: str, destination: Path, expected_sha: str, expected_size: int | None = None):
    if destination.is_file() and sha256(destination) == expected_sha:
        print(f"Verified existing {destination.name}", flush=True)
        return
    temporary = destination.with_suffix(destination.suffix + ".part")
    received = 0
    next_report = 64 * 1024 * 1024
    print(f"Downloading {destination.name}", flush=True)
    with urlopen(Request(url, headers=HEADERS), timeout=60) as response, temporary.open("wb") as target:
        while block := response.read(1024 * 1024):
            target.write(block)
            received += len(block)
            if received > 3_000_000_000 or (expected_size and received > expected_size):
                raise ValueError("Unexpected download size")
            if received >= next_report:
                print(f"  {received // (1024*1024)} MiB received", flush=True)
                next_report += 64 * 1024 * 1024
    if expected_size and received != expected_size:
        raise ValueError("Truncated download; rerun --download")
    if sha256(temporary) != expected_sha:
        raise ValueError("Checksum mismatch; untrusted download will not be used")
    temporary.replace(destination)
    print(f"SHA-256 verified: {destination.name}", flush=True)

def install():
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise ValueError("This bootstrap targets Windows x64; use a native llama.cpp build on this platform")
    CACHE.mkdir(parents=True, exist_ok=True)
    with urlopen(Request(f"https://huggingface.co/api/models/{MODEL_REPO}/revision/{MODEL_REVISION}?blobs=true", headers=HEADERS), timeout=30) as response:
        metadata = json.load(response)
    model_file = next(item for item in metadata["siblings"] if item["rfilename"] == MODEL_NAME)
    expected = model_file["lfs"]["sha256"]
    size = model_file["lfs"]["size"]
    download(RUNTIME_URL, CACHE / RUNTIME_NAME, RUNTIME_SHA256)
    download(f"https://huggingface.co/{MODEL_REPO}/resolve/{MODEL_REVISION}/{MODEL_NAME}", CACHE / MODEL_NAME, expected, size)
    runtime = CACHE / RELEASE
    runtime.mkdir(exist_ok=True)
    with zipfile.ZipFile(CACHE / RUNTIME_NAME) as archive:
        for item in archive.infolist():
            target = (runtime / item.filename).resolve()
            if not target.is_relative_to(runtime.resolve()) or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Unsafe archive path")
        archive.extractall(runtime)
    manifest = {"runtime": RELEASE, "runtime_url": RUNTIME_URL, "runtime_sha256": RUNTIME_SHA256, "model": MODEL_REPO, "model_revision": MODEL_REVISION, "model_file": MODEL_NAME, "model_sha256": expected, "model_size": size, "model_license": metadata.get("cardData", {}).get("license", "See model LICENSE"), "source": f"https://huggingface.co/{MODEL_REPO}"}
    (CACHE / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Local model and runtime installed. Weights remain ignored by Git.", flush=True)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true", help="Download ~1.9 GB from official sources and verify hashes")
    parser.add_argument("--serve", action="store_true", help="Run the installed model on loopback only")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    if args.download:
        install()
    if args.serve:
        if not 1024 <= args.port <= 65535:
            parser.error("port must be 1024..65535")
        servers = list((CACHE / RELEASE).rglob("llama-server.exe"))
        model = CACHE / MODEL_NAME
        if len(servers) != 1 or not model.is_file():
            parser.error("Run --download first")
        print(f"Model endpoint: http://127.0.0.1:{args.port}/v1", flush=True)
        return subprocess.call([str(servers[0]), "-m", str(model), "--host", "127.0.0.1", "--port", str(args.port), "-c", "4096", "-np", "1", "-t", "6", "--jinja", "--reasoning", "off", "--chat-template-kwargs", '{"enable_thinking":false}', "--alias", "astana-local"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not args.download:
        parser.print_help()
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, StopIteration) as error:
        print(f"Local LLM setup failed: {error}", file=sys.stderr)
        raise SystemExit(1)
