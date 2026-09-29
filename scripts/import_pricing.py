"""Import the reviewed public catalog without overwriting configured models."""
import argparse
import json
from pathlib import Path

import httpx
from dotenv import dotenv_values


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    catalog = json.loads((root / "docs/public-pricing.json").read_text(encoding="utf-8"))
    password = dotenv_values(root / ".env").get("ARK_GATEWAY_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("Admin password is not configured in .env")
    with httpx.Client(base_url=args.url, timeout=30) as client:
        client.post("/api/login", json={"password": password}).raise_for_status()
        response = client.get("/api/pricing")
        response.raise_for_status()
        pricing = response.json()
        added = [model for model in catalog["models"] if model not in pricing["models"]]
        for model in added:
            pricing["models"][model] = catalog["models"][model]
        if added:
            saved = client.put("/api/pricing", json=pricing)
            saved.raise_for_status()
            actual = client.get("/api/pricing")
            actual.raise_for_status()
            for model in added:
                for key, value in catalog["models"][model].items():
                    if actual.json()["models"][model].get(key) != value:
                        raise SystemExit(f"Import verification failed: {model}")
        print(f"Imported {len(added)} models; preserved {len(pricing['models']) - len(added)} configured models. Source: {catalog['source']}")


if __name__ == "__main__":
    main()
