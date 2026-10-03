"""Generate private local configuration, optionally importing plan keys."""
from __future__ import annotations

import argparse
import secrets
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import dotenv_values

from .private_files import create_private


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-env", type=Path, help="Existing .env with ARK_*_PLAN_KEYS")
    parser.add_argument("--output", type=Path, default=Path(".env"))
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output exists; refusing to overwrite it")
    source = dotenv_values(args.source_env) if args.source_env else {}
    values = {
        "ARK_GATEWAY_MASTER_KEY": Fernet.generate_key().decode(),
        "ARK_GATEWAY_ADMIN_PASSWORD": secrets.token_urlsafe(24),
        "ARK_GATEWAY_SERVICE_TOKEN": secrets.token_urlsafe(32),
        "ARK_GATEWAY_DB": "data/gateway.db",
        "ARK_AGENT_PLAN_KEYS": source.get("ARK_AGENT_PLAN_KEYS", "") or "",
        "ARK_CODING_PLAN_KEYS": source.get("ARK_CODING_PLAN_KEYS", "") or "",
    }
    with create_private(args.output) as output:
        output.write("".join(f'{k}="{v}"\n' for k, v in values.items()).encode("utf-8"))
    print(f"Created {args.output} with {len([x for x in values['ARK_AGENT_PLAN_KEYS'].split(';') if x.strip()])} Agent and {len([x for x in values['ARK_CODING_PLAN_KEYS'].split(';') if x.strip()])} Coding keys.")


if __name__ == "__main__":
    main()
