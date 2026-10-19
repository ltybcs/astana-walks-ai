"""Start the application and optionally its local LLM, with clean shutdown.

python scripts/run_demo.py --data data/poi.astana.json --local-llm
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from setup_local_llm import server_command

ROOT = Path(__file__).resolve().parents[1]

def wait_ready(url, process, seconds=90):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Local LLM process exited; inspect its output")
        try:
            with urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except (URLError, OSError):
            time.sleep(0.5)
    raise RuntimeError("Local LLM did not become ready in 90 seconds")

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--llm-port", type=int, default=8081)
    parser.add_argument("--local-llm", action="store_true")
    args = parser.parse_args()
    children = []
    env = dict(os.environ)
    try:
        if args.local_llm:
            model = subprocess.Popen(server_command(args.llm_port), cwd=ROOT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            children.append(model)
            wait_ready(f"http://127.0.0.1:{args.llm_port}/health", model)
            env.update(LLM_BASE_URL=f"http://127.0.0.1:{args.llm_port}/v1", LLM_MODEL="astana-local", LLM_JSON_MODE="schema", LLM_TOKEN_PARAMETER="max_tokens", LLM_TIMEOUT_SECONDS="120")
            env.pop("LLM_API_KEY", None)
        command = [sys.executable, "-u", "-m", "app.server", "--port", str(args.port)]
        if args.data:
            command.extend(["--data", str(args.data.resolve())])
        app = subprocess.Popen(command, cwd=ROOT, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        children.append(app)
        print(f"Open http://127.0.0.1:{args.port} — Ctrl+C to stop", flush=True)
        return app.wait()
    except KeyboardInterrupt:
        return 0
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        for child in reversed(children):
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()

if __name__ == "__main__":
    raise SystemExit(main())
