#!/usr/bin/env python3
"""Render a local SDL without storing seed URL credentials in tracked files."""
import argparse
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


def render(image, seed_url, sha256, denom, amount, output):
    if not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", image):
        raise ValueError("Use a registry image pinned with @sha256:<64 lowercase hex digits>")
    parts = urlsplit(seed_url)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("Seed URL must be HTTPS without embedded basic credentials")
    if not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
        raise ValueError("Invalid seed SHA256")
    if not re.fullmatch(r"[A-Za-z0-9/]+", denom) or not re.fullmatch(r"\d+(\.\d+)?", amount) or float(amount)<=0:
        raise ValueError("Invalid price denomination or maximum amount per block")
    text=(Path(__file__).resolve().parents[1]/"deploy/akash/deploy.yaml").read_text()
    text=text.replace("ghcr.io/REPLACE_OWNER/nbappbot@sha256:REPLACE_IMAGE_DIGEST", image)
    text=text.replace("NBABOT_SEED_URL=REPLACE_WITH_HTTPS_SEED_URL", "NBABOT_SEED_URL="+seed_url)
    # Quote the full environment string so #, &, colons and signed query strings stay literal.
    text=text.replace("      - NBABOT_SEED_URL="+seed_url,"      - "+json.dumps("NBABOT_SEED_URL="+seed_url))
    text=text.replace("REPLACE_WITH_64_CHARACTER_SHA256",sha256.lower())
    text=text.replace("denom: uact", "denom: "+denom).replace("amount: 10000", "amount: "+amount)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor=os.open(output,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    with os.fdopen(descriptor,"w") as f:
        f.write(text)
    print(json.dumps({"sdl":str(output.resolve()),"note":"Review actual bids, price denomination, persistent-storage availability, HTTPS and seed access before creating a paid lease."}))


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--image",required=True)
    p.add_argument("--seed-url",default=os.environ.get("NBABOT_SEED_URL"),required=not bool(os.environ.get("NBABOT_SEED_URL")))
    p.add_argument("--sha256",required=True)
    p.add_argument("--denom",default="uact")
    p.add_argument("--max-price",required=True,help="Maximum bid price per block in the selected micro denomination; review Console's estimated real cost")
    p.add_argument("--output",type=Path,default=Path("data/deploy/akash.yaml"))
    args=p.parse_args()
    render(args.image,args.seed_url,args.sha256,args.denom,args.max_price,args.output)
