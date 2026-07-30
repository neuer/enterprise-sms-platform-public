"""容器内 API 探针客户端；不依赖 curl 或外部网络。"""

from __future__ import annotations

import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def probe(kind: str, port: int = 8000) -> int:
    paths = {
        "live": ("/livez", "alive"),
        "ready": ("/readyz", "ready"),
    }
    try:
        path, expected = paths[kind]
    except KeyError:
        return 2
    if not 1 <= port <= 65_535:
        return 2
    request = Request(  # noqa: S310 - 固定回环地址，不接受外部 URL。
        f"http://127.0.0.1:{port}{path}",
        headers={"Connection": "close"},
    )
    try:
        with urlopen(request, timeout=2) as response:  # noqa: S310
            body = json.loads(response.read(128))
            return int(response.status != 200 or body != {"status": expected})
    except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        return 1


def main() -> int:
    if len(sys.argv) not in {2, 3}:
        return 2
    try:
        port = int(sys.argv[2]) if len(sys.argv) == 3 else 8000
    except ValueError:
        return 2
    return probe(sys.argv[1], port)


if __name__ == "__main__":
    raise SystemExit(main())
