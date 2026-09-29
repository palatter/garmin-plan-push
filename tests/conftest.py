"""The suite never writes into the real ~/.config/gpp or the real keychain.

Commands keep recent plans, library entries, push receipts and a debug log
under the home folder; every test gets its own empty ones instead, and an
empty keychain that lives in memory.
"""

import keyring
import pytest
from keyring.backend import KeyringBackend


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.entries.get((service, username))

    def set_password(self, service, username, password):
        self.entries[(service, username)] = password

    def delete_password(self, service, username):
        self.entries.pop((service, username))


@pytest.fixture(autouse=True)
def _own_config_folder(tmp_path_factory, monkeypatch):
    config = tmp_path_factory.mktemp("gpp-config")
    monkeypatch.setattr("gpp.recent.RECENT_DIR", config / "recent")
    monkeypatch.setattr("gpp.library.LIBRARY_DIR", config / "library")
    monkeypatch.setattr("gpp.receipts.RECEIPTS_DIR", config / "pushes")
    monkeypatch.setattr("gpp.cli.LOG_PATH", config / "logs" / "gpp.log")


@pytest.fixture(autouse=True)
def keychain_backend():
    """An empty keychain for each test; the test can read what was saved."""
    memory = MemoryKeyring()
    keyring.set_keyring(memory)
    return memory
