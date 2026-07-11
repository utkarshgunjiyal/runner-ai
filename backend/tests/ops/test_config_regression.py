"""Phase 44 — static config regression guards (defects 1, 2, 9). No secrets printed.

These lock in the local integration fixes: Caddy proxies /threads to the backend,
and the backend MinIO credentials come from the environment (not hardcoded).
Static text/structure checks only — no network, no secret values.
"""

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = Path(__file__).resolve().parents[3]


def test_caddyfile_routes_threads_to_backend():
    # Structure-agnostic: the thread routes must be present and reverse-proxied to
    # the backend upstream (works whether they are on the @backend matcher line or
    # in a dedicated handle block).
    caddy = (_ROOT / "deploy" / "Caddyfile").read_text()
    assert "/threads" in caddy
    assert "/threads/*" in caddy
    assert "{$BACKEND_UPSTREAM}" in caddy  # backend is reverse-proxied


def test_compose_backend_minio_creds_are_not_hardcoded():
    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
    app_env = compose["x-app-environment"]
    access = str(app_env["MINIO_ACCESS_KEY"])
    secret = str(app_env["MINIO_SECRET_KEY"])
    # Must be an env substitution, not a literal credential.
    assert access.startswith("${MINIO_ACCESS_KEY")
    assert secret.startswith("${MINIO_SECRET_KEY")
    assert access != "minioadmin" and secret != "minioadmin"


def test_compose_minio_bucket_and_secure_from_env():
    compose = yaml.safe_load((_ROOT / "docker-compose.yml").read_text())
    app_env = compose["x-app-environment"]
    assert str(app_env["MINIO_BUCKET"]).startswith("${MINIO_BUCKET")
    assert str(app_env["MINIO_SECURE"]).startswith("${MINIO_SECURE")
