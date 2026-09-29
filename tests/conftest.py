"""The suite never writes into the real ~/.config/gpp.

Commands keep recent plans, library entries, push receipts and a debug log
under the home folder; every test gets its own empty ones instead.
"""

import pytest


@pytest.fixture(autouse=True)
def _own_config_folder(tmp_path_factory, monkeypatch):
    config = tmp_path_factory.mktemp("gpp-config")
    monkeypatch.setattr("gpp.recent.RECENT_DIR", config / "recent")
    monkeypatch.setattr("gpp.library.LIBRARY_DIR", config / "library")
    monkeypatch.setattr("gpp.receipts.RECEIPTS_DIR", config / "pushes")
    monkeypatch.setattr("gpp.cli.LOG_PATH", config / "logs" / "gpp.log")
