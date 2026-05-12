"""Tests for router.py — classify_url()."""

import pytest
from router import classify_url


# ---------------------------------------------------------------------------
# YouTube (long)
# ---------------------------------------------------------------------------


def test_youtube_watch_no_scheme_prefix():
    result = classify_url("https://youtube.com/watch?v=abc")
    assert result["type"] == "long"
    assert result["force"] is False


def test_youtu_be():
    result = classify_url("https://youtu.be/abc")
    assert result["type"] == "long"
    assert result["force"] is False


def test_youtube_watch_with_www_and_query():
    result = classify_url("https://www.youtube.com/watch?v=xyz&t=30s")
    assert result["type"] == "long"
    assert result["force"] is False


# ---------------------------------------------------------------------------
# Short-form (non-YouTube)
# ---------------------------------------------------------------------------


def test_tiktok_is_short():
    result = classify_url("https://www.tiktok.com/@user/video/123")
    assert result["type"] == "short"
    assert result["force"] is False


def test_instagram_is_short():
    result = classify_url("https://instagram.com/reel/abc")
    assert result["type"] == "short"
    assert result["force"] is False


def test_twitter_is_short():
    result = classify_url("https://twitter.com/user/status/123")
    assert result["type"] == "short"
    assert result["force"] is False


# ---------------------------------------------------------------------------
# Validation errors
# ---------------------------------------------------------------------------


def test_no_scheme_raises():
    with pytest.raises(ValueError):
        classify_url("example.com/video")


def test_ftp_scheme_raises():
    with pytest.raises(ValueError):
        classify_url("ftp://example.com")


def test_localhost_raises():
    with pytest.raises(ValueError):
        classify_url("http://localhost/video")


def test_127_0_0_1_raises():
    with pytest.raises(ValueError):
        classify_url("http://127.0.0.1/video")


def test_private_ip_192_168_raises():
    with pytest.raises(ValueError):
        classify_url("http://192.168.1.1/video")


def test_private_ip_10_raises():
    with pytest.raises(ValueError):
        classify_url("http://10.0.0.1/video")


# ---------------------------------------------------------------------------
# /refresh command
# ---------------------------------------------------------------------------


def test_refresh_tiktok_is_short_force():
    result = classify_url("/refresh https://tiktok.com/video")
    assert result["type"] == "short"
    assert result["force"] is True
    assert result["url"] == "https://tiktok.com/video"


def test_refresh_youtu_be_is_long_force():
    result = classify_url("/refresh https://youtu.be/abc")
    assert result["type"] == "long"
    assert result["force"] is True
    assert result["url"] == "https://youtu.be/abc"


def test_refresh_no_url_raises():
    with pytest.raises(ValueError):
        classify_url("/refresh")


def test_malformed_string_raises():
    with pytest.raises(ValueError):
        classify_url("not a url at all")
