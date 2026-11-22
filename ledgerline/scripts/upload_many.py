"""Upload a batch of fresh synthetic invoices, to make work for the workers (guide Chapter 6).

    # local stack
    docker compose run --rm api python -m scripts.upload_many --count 20 --url http://api:8000

    # staging (the site password is read from the environment, never from the command line)
    docker compose run --rm -e SITE_PASSWORD api python -m scripts.upload_many --count 40 --url https://xxxx.cloudfront.net

Each run generates NEW invoices (a random seed), because uploading the same
file twice is, by design, the same invoice (app/main.py). It signs in with
the development login, so it only works where DEV_LOGIN is on: local
development (and staging before Chapter 7 turned the dev login off there). Uploads are quick; the extraction happens later, on
the workers, which is the point.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--email", default="priya@acme.example", help="which seeded user uploads")
    args = parser.parse_args()

    out = Path(tempfile.mkdtemp(prefix="ll-upload-"))
    seed = random.randint(1000, 10**9)  # noqa: S311 (not security-sensitive)
    subprocess.run([sys.executable, "-m", "scripts.generate_invoices", "--count", str(args.count),
                    "--seed", str(seed), "--out", str(out)], check=True, capture_output=True)
    files = [out / item["file"] for item in json.loads((out / "manifest.json").read_text())["invoices"]]

    password = os.getenv("SITE_PASSWORD")
    auth = ("ledgerline", password) if password else None  # HTTP Basic: any username, the site password
    with httpx.Client(base_url=args.url.rstrip("/"), auth=auth, timeout=30, follow_redirects=False) as client:
        page = client.get("/login")
        page.raise_for_status()
        match = re.search(r'value="([0-9a-f-]{36})"[^<]*>\s*[^<]*<span class="muted">[^<]*' + re.escape(args.email), page.text)
        if not match:
            sys.exit(f"No user {args.email} on the sign-in page. Is DEV_LOGIN on, and the demo data seeded?")
        if client.post("/login", data={"user_id": match.group(1)}).status_code != 303:
            sys.exit("Sign-in failed.")
        # Every form carries a CSRF token tied to the session (Chapter 8); read it like a browser would.
        token = re.search(r'name="csrf_token" value="([0-9a-f]+)"', client.get("/invoices").text)
        if not token:
            sys.exit("No CSRF token on /invoices.")

        started = time.monotonic()
        for i, path in enumerate(files, 1):
            response = client.post("/invoices", data={"csrf_token": token.group(1)},
                                   files={"file": (path.name, path.read_bytes())})
            if response.status_code != 303:
                sys.exit(f"Upload {i} failed: HTTP {response.status_code} {response.text[:200]}")
        elapsed = time.monotonic() - started
    print(f"Uploaded {len(files)} invoices in {elapsed:.1f} s ({elapsed / len(files) * 1000:.0f} ms each). "
          "Now watch the queue: python -m app.jobs status")


if __name__ == "__main__":
    main()
