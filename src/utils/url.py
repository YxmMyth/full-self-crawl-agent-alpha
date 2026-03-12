"""URL utilities — single source of truth for URL comparison and domain matching."""


def normalize_url(url: str) -> str:
    """Strip fragment and trailing slash for consistent URL comparison.

    Used across scheduler, orchestrator, controller, and run_intelligence
    to ensure the same URL is never treated as two different URLs.
    """
    if not url:
        return url
    return url.split("#")[0].rstrip("/")


def is_same_domain(netloc: str, target_domain: str) -> bool:
    """Check if netloc matches target_domain exactly or is a subdomain.

    Strips 'www.' prefix from both sides before comparison.
    Returns True for exact match or subdomain match:
        is_same_domain("example.com", "example.com")       → True
        is_same_domain("api.example.com", "example.com")    → True
        is_same_domain("notexample.com", "example.com")     → False
    """
    netloc = netloc.lower().split(":")[0].lstrip("www.")
    target = target_domain.lower().lstrip("www.")
    return netloc == target or netloc.endswith("." + target)
