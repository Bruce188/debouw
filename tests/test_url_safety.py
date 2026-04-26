"""Tests for debouw.ingest.url_safety SSRF allowlist helper."""

import pytest

from debouw.ingest.url_safety import is_inzageloket_attachment_allowed

_ALLOWED_HOST = "omgevingsloketinzage.omgeving.vlaanderen.be"


def test_allowed_host():
    url = f"https://{_ALLOWED_HOST}/path/to/file.pdf"
    assert is_inzageloket_attachment_allowed(url) is True


def test_rejected_host_evil_example():
    assert is_inzageloket_attachment_allowed("https://evil.example.com/exfil") is False


def test_rejected_scheme_file():
    assert is_inzageloket_attachment_allowed("file:///etc/passwd") is False


def test_rejected_scheme_javascript():
    assert is_inzageloket_attachment_allowed("javascript:alert(1)") is False


def test_rejected_empty_url():
    assert is_inzageloket_attachment_allowed("") is False


def test_case_insensitive_host():
    url = f"https://OMGEVINGSLOKETINZAGE.OMGEVING.VLAANDEREN.BE/file.pdf"
    assert is_inzageloket_attachment_allowed(url) is True


def test_rejected_subdomain_not_in_allowlist():
    url = f"https://malicious.{_ALLOWED_HOST}/file.pdf"
    assert is_inzageloket_attachment_allowed(url) is False
