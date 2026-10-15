#!/usr/bin/env python3
"""Expand a shortened URL to the full destination URL.

Accepts a tiny URL (TinyURL, bit.ly, t.co, and any other link that
redirects) and prints the absolute URL it resolves to.
"""

from __future__ import annotations

import argparse
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_REDIRECTS = 10
USER_AGENT = "expand-tiny-url/1.0"
# Statuses that often mean "HEAD is not implemented"; retry with GET.
_HEAD_UNSUPPORTED = {400, 403, 405, 501}


class _RedirectFound(Exception):
    """Raised when a response points at the next URL in the chain."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.url = url


class _CaptureRedirect(HTTPRedirectHandler):
    """Stop at each hop so only http(s) targets are followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            fp.read()
        finally:
            fp.close()
        raise _RedirectFound(newurl)


def expand_tiny_url(
    url: str,
    *,
    timeout: float = 10.0,
    max_redirects: int = MAX_REDIRECTS,
) -> str:
    """Return the absolute URL that ``url`` redirects to.

    The input may omit a scheme (``https`` is assumed). Redirects are
    followed one hop at a time, and only ``http`` and ``https`` targets
    are accepted. A URL that does not redirect is returned unchanged.
    """
    if max_redirects < 0:
        raise ValueError("max_redirects must be zero or greater")

    current = _require_http_url(_with_scheme(url.strip()))
    opener = build_opener(_CaptureRedirect())
    followed = 0

    while True:
        nxt = _next_hop(opener, current, timeout)
        if nxt is None:
            return current
        followed += 1
        if followed > max_redirects:
            raise ValueError(f"exceeded {max_redirects} redirects for {url!r}")
        current = _require_http_url(urljoin(current, nxt))


def _with_scheme(url: str) -> str:
    if not url:
        raise ValueError("URL is empty")
    if urlparse(url).scheme:
        return url
    return "https://" + url


def _require_http_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"expected an http or https URL, got {url!r}")
    return url


def _next_hop(opener, url: str, timeout: float) -> str | None:
    """Return the redirect target, or None when ``url`` is the final hop."""
    last_error: HTTPError | URLError | None = None
    for method in ("HEAD", "GET"):
        request = Request(url, method=method, headers={"User-Agent": USER_AGENT})
        try:
            with opener.open(request, timeout=timeout) as response:
                response.close()
                return None
        except _RedirectFound as found:
            return found.url
        except HTTPError as exc:
            exc.close()
            # 3xx here means urllib refused the hop (for example a file: target).
            # A normal http(s) redirect is delivered as _RedirectFound instead.
            if 300 <= exc.code < 400 and exc.code != 304:
                raise
            last_error = exc
            if method == "HEAD" and exc.code in _HEAD_UNSUPPORTED:
                continue
            return None
        except URLError as exc:
            last_error = exc
            if method == "HEAD":
                continue
            raise
    if last_error is not None:
        raise last_error
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Expand a shortened URL and print the full destination URL."
    )
    parser.add_argument(
        "url",
        help="Shortened URL, for example https://tinyurl.com/example",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="Seconds to wait for each request (default: 10)",
    )
    parser.add_argument(
        "--max-redirects",
        type=int,
        default=MAX_REDIRECTS,
        help=f"Maximum redirects to follow (default: {MAX_REDIRECTS})",
    )
    args = parser.parse_args(argv)

    try:
        print(expand_tiny_url(
            args.url,
            timeout=args.timeout,
            max_redirects=args.max_redirects,
        ))
    except (ValueError, HTTPError, URLError, TimeoutError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
