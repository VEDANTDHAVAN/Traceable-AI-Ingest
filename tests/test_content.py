import pytest

from ai_ingest.content import (
    canonical_url,
    content_identity,
    extract_article,
    normalize_whitespace,
)


ARTICLE = """
<html><head>
<title>Fallback title</title>
<meta property="og:title" content="Model release">
<meta property="article:published_time" content="2026-11-23T09:00:00Z">
<link rel="canonical" href="https://example.com/news/model-release?utm_source=feed">
</head><body>
<nav>menu</nav>
<article>
<p>""" + ("This is a substantial paragraph about an AI model. " * 8) + """</p>
<p>Short.</p>
</article>
<script>ignore()</script>
</body></html>
"""


def test_extract_article_metadata_and_text():
    result = extract_article(ARTICLE)

    assert result["title"] == "Model release"
    assert result["published_at"].isoformat() == "2026-11-23T09:00:00+00:00"
    assert result["canonical_url"].startswith("https://example.com/news/model-release")
    assert "substantial paragraph" in result["text"]
    assert "ignore()" not in result["text"]
    assert "menu" not in result["text"]
    assert result["extraction_status"] == "FULL_TEXT"


def test_missing_metadata_stays_none():
    result = extract_article("<html><body><p>" + "text " * 100 + "</p></body></html>")
    assert result["published_at"] is None
    assert result["canonical_url"] is None


def test_canonical_url_strips_tracking_and_www():
    assert canonical_url("https://www.Example.com/path/?utm_source=x") == (
        "https://example.com/path"
    )
    with pytest.raises(ValueError):
        canonical_url("ftp://example.com/x")


def test_content_identity_collapses_whitespace_and_case():
    assert content_identity("The  AI Model\n\nshipped") == content_identity(
        "the ai model shipped"
    )
    assert normalize_whitespace(" a\n b ") == "a b"
