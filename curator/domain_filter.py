"""Domain whitelist/blacklist filtering for external search results.

Config:
  CURATOR_ALLOWED_DOMAINS — comma-separated; if set, only results from these
                            domains are kept (in addition to entries without URLs).
  CURATOR_BLOCKED_DOMAINS — comma-separated; results from these domains are
                            always removed. Blocked takes precedence over allowed.

Domain matching rules:
  - www. prefix is normalised away on both URL and config entry.
  - A config entry "example.com" matches the domain itself AND all subdomains
    (sub.example.com, www.example.com …).
  - A config entry "sub.example.com" matches only that subdomain and its children,
    NOT the bare parent "example.com".
  - Matching is case-insensitive.
"""

import logging
import re
from urllib.parse import urlparse

log = logging.getLogger("curator")

# ── Core helpers ──────────────────────────────────────────────────────────────


def extract_domain(url: str) -> str:
    """Return the normalised (lowercased, www-stripped) domain from *url*.

    Returns "" for empty input or URLs that cannot be parsed meaningfully.
    """
    if not url or not url.strip():
        return ""

    raw = url.strip()

    # Add a scheme so urlparse works for bare domains like "example.com"
    if "://" not in raw and not raw.startswith("//"):
        raw = "https://" + raw

    try:
        parsed = urlparse(raw)
        host = parsed.hostname or ""
        # Reject parse results where the "host" is just the scheme we injected
        # or there's no meaningful path/host structure.
        if host in ("https", "http", "") and not parsed.path.strip("/"):
            return ""
    except Exception as e:
        log.debug("failed to parse URL %r: %s", raw, e)
        return ""

    if not host:
        return ""

    # Strip leading www. (only bare www., not www2. etc.)
    if host.startswith("www."):
        host = host[4:]

    return host.lower()


def domain_matches(url: str, domain_list: list) -> bool:
    """Return True if *url*'s domain matches any entry in *domain_list*.

    Matching is exact-or-subdomain: config entry "example.com" matches
    "example.com" and "sub.example.com" but NOT "notexample.com".
    """
    if not domain_list:
        return False

    host = extract_domain(url)
    if not host:
        return False

    for entry in domain_list:
        if not entry:
            continue
        # Normalise config entry the same way
        norm = entry.strip().lower()
        if norm.startswith("www."):
            norm = norm[4:]

        if host == norm or host.endswith("." + norm):
            return True

    return False


# ── Structured result filtering (DDG / Tavily) ────────────────────────────────


def filter_results_by_domain(
    results: list,
    url_key: str,
    allowed: list,
    blocked: list,
) -> list:
    """Filter a list of result dicts by domain rules.

    Args:
        results:  List of dicts (e.g. DDG or Tavily result items).
        url_key:  Key inside each dict that holds the URL ("href" for DDG,
                  "url" for Tavily).
        allowed:  Whitelist. If non-empty, only results whose URL matches are
                  kept — PLUS results that have no URL key (can't determine,
                  kept by default).
        blocked:  Blacklist. Results matching any blocked domain are removed.
                  Takes precedence over *allowed*.

    Returns a new list; input is not modified.
    """
    if not results:
        return []
    if not allowed and not blocked:
        return list(results)

    out = []
    for item in results:
        url = item.get(url_key, "")

        if not url:
            # No URL to check — keep by default (can't determine domain).
            out.append(item)
            continue

        if blocked and domain_matches(url, blocked):
            continue  # Blocked domain — drop.

        if allowed and not domain_matches(url, allowed):
            continue  # Allowed-only mode and URL doesn't qualify — drop.

        out.append(item)

    return out


# ── Text-level filtering (Grok / OAI free-form output) ───────────────────────

_URL_RE = re.compile(r"https?://[^\s\)\]\"'>]+", re.IGNORECASE)


def filter_text_by_domain(text: str, allowed: list, blocked: list) -> str:
    """Filter free-form text by removing lines that reference blocked/non-allowed domains.

    Lines that contain NO URL are always preserved (they carry context).
    Lines that contain at least one URL are checked:
      - If any URL in the line matches a blocked domain → line dropped.
      - If allowed is set and NO URL in the line matches an allowed domain → line dropped.

    Args:
        text:    Free-form multi-line string (e.g. Grok/OAI search result).
        allowed: Whitelist. If empty, no whitelist filtering.
        blocked: Blacklist. If empty, no blacklist filtering.

    Returns the filtered text with the same line endings.
    """
    if not text:
        return text
    if not allowed and not blocked:
        return text

    out_lines = []
    for line in text.splitlines(keepends=True):
        urls = _URL_RE.findall(line)
        if not urls:
            # No URL → always keep.
            out_lines.append(line)
            continue

        # Blocked check (any URL from a blocked domain → drop line).
        if blocked and any(domain_matches(u, blocked) for u in urls):
            continue

        # Allowed check (if whitelist set, at least one URL must match).
        if allowed and not any(domain_matches(u, allowed) for u in urls):
            continue

        out_lines.append(line)

    return "".join(out_lines)


# ── Prompt hint builder ───────────────────────────────────────────────────────


def build_domain_prompt_hint(allowed: list, blocked: list) -> str:
    """Build a short natural-language hint to inject into search prompts.

    Returns "" if both lists are empty (no-op).
    """
    parts = []
    if blocked:
        domains = ", ".join(blocked)
        parts.append(f"不要引用以下域名的内容：{domains}。")
    if allowed:
        domains = ", ".join(allowed)
        parts.append(f"优先引用来自以下域名的内容：{domains}。")
    return " ".join(parts)
