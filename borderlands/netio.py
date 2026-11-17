"""One tiny JSON-over-HTTPS helper, so the network code can be tested with a fake."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

# A generic client name: nothing about the user or their machine is ever sent.
USER_AGENT = "borderlands-mo2-plugin"


class NetworkError(Exception):
    """The server could not be reached or did not answer with JSON."""


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
