"""
tests/test_domain_filter.py — Unit tests for domain whitelist/blacklist filtering.
"""

import pytest

from curator.domain_filter import (
    build_domain_prompt_hint,
    domain_matches,
    extract_domain,
    filter_results_by_domain,
    filter_text_by_domain,
)

# ─── extract_domain ───────────────────────────────────────────────────────────


class TestExtractDomain:
    def test_simple_url(self):
        assert extract_domain("https://example.com/foo") == "example.com"

    def test_strips_www(self):
        assert extract_domain("https://www.example.com/bar") == "example.com"

    def test_subdomain_preserved(self):
        assert extract_domain("https://docs.example.com/page") == "docs.example.com"

    def test_http(self):
        assert extract_domain("http://evil.com/path?q=1") == "evil.com"

    def test_no_scheme(self):
        # bare domain with no scheme — best-effort
        assert extract_domain("example.com") == "example.com"

    def test_invalid_returns_empty(self):
        assert extract_domain("not a url at all !@#") == ""

    def test_empty_string(self):
        assert extract_domain("") == ""


# ─── domain_matches ───────────────────────────────────────────────────────────


class TestDomainMatches:
    def test_exact_match(self):
        assert domain_matches("https://example.com/foo", ["example.com"])

    def test_www_normalized(self):
        # URL has www, config entry has bare domain
        assert domain_matches("https://www.example.com/", ["example.com"])

    def test_subdomain_matches_parent_config(self):
        # config entry is example.com → blocks subdomains too
        assert domain_matches("https://sub.example.com/page", ["example.com"])

    def test_specific_subdomain_in_config(self):
        # config only lists sub.example.com → does NOT match bare example.com
        assert not domain_matches("https://example.com/page", ["sub.example.com"])

    def test_no_match(self):
        assert not domain_matches("https://safe.com/page", ["evil.com"])

    def test_empty_list(self):
        assert not domain_matches("https://anything.com", [])

    def test_multiple_domains_one_matches(self):
        assert domain_matches("https://evil.com/x", ["safe.com", "evil.com"])

    def test_case_insensitive(self):
        assert domain_matches("https://EXAMPLE.COM/path", ["example.com"])


# ─── filter_results_by_domain ─────────────────────────────────────────────────


class TestFilterResultsByDomain:
    RESULTS = [
        {"href": "https://good.com/a", "title": "Good A", "body": "ok"},
        {"href": "https://evil.com/b", "title": "Evil B", "body": "bad"},
        {"href": "https://neutral.com/c", "title": "Neutral C", "body": "meh"},
        {"href": "https://also-good.com/d", "title": "Good D", "body": "fine"},
    ]

    def test_no_filters_returns_all(self):
        out = filter_results_by_domain(self.RESULTS, "href", [], [])
        assert len(out) == 4

    def test_blocked_domain_removed(self):
        out = filter_results_by_domain(self.RESULTS, "href", [], ["evil.com"])
        urls = [r["href"] for r in out]
        assert "https://evil.com/b" not in urls
        assert len(out) == 3

    def test_allowed_only_keeps_matching(self):
        out = filter_results_by_domain(self.RESULTS, "href", ["good.com", "also-good.com"], [])
        assert len(out) == 2
        assert all("good.com" in r["href"] for r in out)

    def test_blocked_takes_precedence_over_allowed(self):
        # evil.com is both in allowed and blocked → should be blocked
        out = filter_results_by_domain(self.RESULTS, "href", ["evil.com", "good.com"], ["evil.com"])
        urls = [r["href"] for r in out]
        assert "https://evil.com/b" not in urls

    def test_empty_results(self):
        assert filter_results_by_domain([], "href", ["safe.com"], ["evil.com"]) == []

    def test_missing_url_key_entry_kept(self):
        results = [{"title": "no href"}, {"href": "https://evil.com/x", "title": "bad"}]
        out = filter_results_by_domain(results, "href", [], ["evil.com"])
        # entry without href key is kept (can't determine domain → safe default)
        assert len(out) == 1
        assert out[0]["title"] == "no href"

    def test_alternate_url_key(self):
        results = [
            {"url": "https://evil.com/p", "title": "Bad"},
            {"url": "https://good.com/q", "title": "Good"},
        ]
        out = filter_results_by_domain(results, "url", [], ["evil.com"])
        assert len(out) == 1
        assert out[0]["title"] == "Good"


# ─── filter_text_by_domain ────────────────────────────────────────────────────


class TestFilterTextByDomain:
    def test_no_filters_passthrough(self):
        text = "Some text with https://example.com/page mentioned."
        assert filter_text_by_domain(text, [], []) == text

    def test_blocked_domain_line_removed(self):
        text = "Good content here.\n" "Source: https://evil.com/article — bad stuff.\n" "More good content.\n"
        out = filter_text_by_domain(text, [], ["evil.com"])
        assert "evil.com" not in out
        assert "Good content here." in out
        assert "More good content." in out

    def test_allowed_only_lines_kept(self):
        text = "From https://trusted.com/doc — valid.\n" "From https://random.com/page — unknown.\n"
        out = filter_text_by_domain(text, ["trusted.com"], [])
        # Lines without URLs are always kept; only URL-containing lines outside allowed are dropped
        assert "trusted.com" in out
        assert "random.com" not in out

    def test_line_without_url_always_kept(self):
        text = "No URL on this line.\nAnother safe line.\n"
        out = filter_text_by_domain(text, ["trusted.com"], ["evil.com"])
        assert out.strip() == text.strip()

    def test_empty_text(self):
        assert filter_text_by_domain("", ["safe.com"], ["evil.com"]) == ""


# ─── build_domain_prompt_hint ─────────────────────────────────────────────────


class TestBuildDomainPromptHint:
    def test_no_domains_returns_empty(self):
        assert build_domain_prompt_hint([], []) == ""

    def test_blocked_only(self):
        hint = build_domain_prompt_hint([], ["spam.com", "ads.net"])
        assert "spam.com" in hint
        assert "ads.net" in hint
        assert "avoid" in hint.lower() or "block" in hint.lower() or "exclude" in hint.lower() or "不要" in hint

    def test_allowed_only(self):
        hint = build_domain_prompt_hint(["docs.python.org", "github.com"], [])
        assert "docs.python.org" in hint
        assert "github.com" in hint

    def test_both(self):
        hint = build_domain_prompt_hint(["trusted.com"], ["spam.com"])
        assert "trusted.com" in hint
        assert "spam.com" in hint
