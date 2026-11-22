"""Download sqlglot at the pinned commit and unpack it to /opt/sqlglot.

Runs during `docker build` (the only phase with network). The base image has
no git or curl, so this uses the standard library only.
"""

import io
import shutil
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
URL = f"https://github.com/tobymao/sqlglot/archive/{COMMIT}.tar.gz"
DEST = Path(sys.argv[1] if len(sys.argv) > 1 else "/opt/sqlglot")


def main() -> None:
    last_error = None
    for attempt in range(5):
        try:
            with urllib.request.urlopen(URL, timeout=120) as response:
                data = response.read()
            break
        except Exception as error:  # network hiccup: retry
            last_error = error
            time.sleep(2 + attempt * 3)
    else:
        raise SystemExit(f"download failed: {last_error}")

    tmp = DEST.with_name(DEST.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        archive.extractall(tmp, filter="data")
    (top,) = list(tmp.iterdir())
    shutil.rmtree(DEST, ignore_errors=True)
    top.rename(DEST)
    tmp.rmdir()
    print(f"sqlglot {COMMIT[:7]} -> {DEST}", file=sys.stderr)


if __name__ == "__main__":
    main()
