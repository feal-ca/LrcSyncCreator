"""Suite-wide safety nets.

The test suite must never log in to the real Soulseek network, nor read
the user's real Soulseek account from an old slskd-bin/slskd.yml next to
the code. Tests that exercise those paths patch in fakes explicitly.
"""

import sys

import pytest


@pytest.fixture(autouse=True)
def _no_real_soulseek(monkeypatch, tmp_path):
    try:
        import slsk_client
        from soulseek import SlskdConnectionError
    except ImportError:
        return

    def refuse(self, *a, **k):
        raise SlskdConnectionError("The real Soulseek network is off-limits in tests.")

    monkeypatch.setattr(slsk_client.SlskClient, "start", refuse)
    if "app" in sys.modules:
        monkeypatch.setattr(sys.modules["app"], "SLSKD_CONFIG_PATH", str(tmp_path / "no-slskd.yml"))
    else:
        import app  # noqa: F401 — imported here so the path can be patched for every test

        monkeypatch.setattr(app, "SLSKD_CONFIG_PATH", str(tmp_path / "no-slskd.yml"))
