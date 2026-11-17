"""One tiny JSON-over-HTTPS helper, so the network code can be tested with a fake."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

# A generic client name: nothing about the user or their machine is ever sent.
USER_AGENT = "borderlands-mo2-plugin"


class NetworkError(Exception):
    """The server could not be reached or did not answer with JSON."""


class Cancelled(Exception):
    """The user cancelled a download."""


class ChecksumError(Exception):
    """A download does not match its published SHA-256."""


def request_json(url: str, data=None, headers: dict | None = None, timeout: float = 8.0):
    """GET (or POST ``data`` as JSON) and return ``(http_status, parsed_json_or_None)``.

    HTTP error statuses are returned, not raised; only connection problems raise
    :class:`NetworkError`.
    """
    body = json.dumps(data).encode("utf-8") if data is not None else None
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read().decode("utf-8"))
        except (ValueError, OSError):
            return err.code, None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        raise NetworkError(str(err)) from err


def download(url: str, dest: Path, expected_sha256: str = "", progress=None, opener=None,
             timeout: float = 20.0, chunk: int = 1 << 16) -> str:
    """Stream ``url`` into ``dest`` and return its SHA-256.

    Written to ``<dest>.part`` first and moved into place only once complete and, when
    ``expected_sha256`` is given, matching it.  ``progress(done, total)`` is called as data
    arrives (``total`` is 0 when unknown); returning False cancels.  Only https is accepted,
    including after redirects.
    """
    if not url.startswith("https://"):
        raise NetworkError("only https downloads are allowed")
    dest = Path(dest)
    part = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    digest = hashlib.sha256()
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
            if not response.geturl().startswith("https://"):
                raise NetworkError("the download was redirected away from https")
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            with open(part, "wb") as out:
                while block := response.read(chunk):
                    out.write(block)
                    digest.update(block)
                    done += len(block)
                    if progress is not None and progress(done, total) is False:
                        raise Cancelled()
    except (Cancelled, NetworkError):
        _discard(part)
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
        _discard(part)
        raise NetworkError(str(err)) from err
    if expected_sha256 and digest.hexdigest() != expected_sha256.lower():
        _discard(part)
        raise ChecksumError("the download does not match the checksum GitHub publishes for it")
    os.replace(part, dest)
    return digest.hexdigest()


def _discard(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
