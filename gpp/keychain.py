"""API keys and the Garmin password in the operating system's keychain.

That is Windows Credential Manager, the macOS Keychain, or the Secret
Service on Linux, reached through the keyring package. The web app's key
fields and a Garmin password typed into gpp are saved there, so nobody has
to set an environment variable and open a new terminal before a key works.
An environment variable still wins over the keychain, so a key set with
setx or export keeps working as it did.

Entries are filed under the service name "garmin-plan-push": a key under the
name of the environment variable it stands in for (ANTHROPIC_API_KEY), a
Garmin password under "garmin:" and the account's email.

A keychain that cannot be used (none on a headless Linux box, a locked one,
a refused prompt) reads as empty and is logged, never raised; only saving
raises, so the caller can tell the user the value was not kept.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys

log = logging.getLogger("gpp.keychain")

SERVICE = "garmin-plan-push"
GARMIN_PREFIX = "garmin:"
# The accounts whose Garmin password is saved: the keychain cannot be
# listed, and signing out has to find every one of them.
GARMIN_ACCOUNTS = "garmin-accounts"
# What a key may be filed under: an environment variable's name.
KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class KeychainError(RuntimeError):
    """The value could not be saved in, or removed from, the keychain."""


def _keyring():
    """The keyring module when it has a usable backend, else None."""
    try:
        import keyring
        from keyring.backends import fail
    except ImportError:  # pragma: no cover - keyring is a dependency
        return None
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # a broken backend is "no keychain"
        log.debug("no keychain: %s", exc)
        return None
    if isinstance(backend, fail.Keyring) or type(backend).__module__.endswith(".null"):
        return None
    return keyring


def available() -> bool:
    return _keyring() is not None


def where() -> str:
    """The keychain's name as the user knows it, for messages."""
    if sys.platform == "win32":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "the macOS Keychain"
    return "the system keyring"


def get(name: str) -> str | None:
    keyring = _keyring()
    if keyring is None:
        return None
    try:
        return keyring.get_password(SERVICE, name) or None
    except Exception as exc:  # locked, refused or broken: not there
        log.debug("could not read %s from the keychain: %s", name, exc)
        return None


def put(name: str, value: str) -> None:
    keyring = _keyring()
    if keyring is None:
        raise KeychainError(f"there is no keychain to save it in on this computer ({where()})")
    try:
        keyring.set_password(SERVICE, name, value)
    except Exception as exc:  # any backend failure is the same to the user
        raise KeychainError(f"could not save it in {where()}: {exc}") from exc


def forget(name: str) -> bool:
    """Remove `name`; True when there was something to remove."""
    keyring = _keyring()
    if keyring is None or get(name) is None:
        return False
    try:
        keyring.delete_password(SERVICE, name)
    except Exception as exc:
        raise KeychainError(f"could not remove it from {where()}: {exc}") from exc
    return True


# --- keys ---------------------------------------------------------------------


def lookup(env_name: str) -> str | None:
    """A key by its environment variable's name: the variable, else the keychain."""
    return os.environ.get(env_name) or get(env_name)


def source(env_name: str) -> str | None:
    """Where lookup() finds this key: "environment", "keychain" or None."""
    if os.environ.get(env_name):
        return "environment"
    return "keychain" if get(env_name) else None


def check_key_name(name: str) -> str:
    if not KEY_NAME.match(name or ""):
        raise KeychainError(
            f"{name!r} is not a key name; use the environment variable's name, "
            "like ANTHROPIC_API_KEY"
        )
    return name


# --- Garmin passwords ---------------------------------------------------------


def _account(email: str) -> str:
    return email.strip().lower()


def garmin_password(email: str) -> str | None:
    return get(GARMIN_PREFIX + _account(email)) if email.strip() else None


def save_garmin_password(email: str, password: str) -> None:
    account = _account(email)
    put(GARMIN_PREFIX + account, password)
    accounts = garmin_accounts()
    if account not in accounts:
        put(GARMIN_ACCOUNTS, json.dumps(sorted([*accounts, account])))


def forget_garmin_passwords() -> int:
    """Remove every saved Garmin password; how many there were."""
    removed = sum(forget(GARMIN_PREFIX + account) for account in garmin_accounts())
    forget(GARMIN_ACCOUNTS)
    return removed


def garmin_accounts() -> list[str]:
    """The accounts whose Garmin password is saved, lowercased."""
    try:
        listed = json.loads(get(GARMIN_ACCOUNTS) or "[]")
    except ValueError:
        return []
    return [a for a in listed if isinstance(a, str)] if isinstance(listed, list) else []
