"""Refresh the local POI cache once, or watch with a daily TTL.

python scripts/refresh_poi.py
python scripts/refresh_poi.py --force  # Still observes the 1-hour cooldown.
python scripts/refresh_poi.py --watch
No HTTP endpoint or user coordinate triggers this operation.
"""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.data_refresh import DatasetStore, POIRefresher
from app.retrieval import Retriever


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--watch", action="store_true")
    args = parser.parse_args()
    store = DatasetStore(Retriever(args.data))
    refresher = POIRefresher(store)
    try:
        if args.watch:
            refresher.start()
            while True:
                time.sleep(1)
        else:
            refreshed = refresher.refresh_once(force=args.force)
            print(json.dumps({"refreshed": refreshed, "places_count": len(store.snapshot().places), "refresh": refresher.status()}, ensure_ascii=False))
            return 1 if refresher.status()["state"] == "error" else 0
    except KeyboardInterrupt:
        return 0
    finally:
        refresher.stop()


if __name__ == "__main__":
    raise SystemExit(main())
