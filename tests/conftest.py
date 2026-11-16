"""Fixtures shared by every test module."""

import pytest


@pytest.fixture(autouse=True)
def maestro_home(tmp_path, monkeypatch):
    """Give every test its own MAESTRO_HOME, so none reads or writes the real one.

    The store, the settings and the backends file all live under it, and a
    test that forgot to isolate itself would otherwise rewrite the owner's.
    monkeypatch puts the previous value back however the test ends.
    """
    home = tmp_path / "home"
    monkeypatch.setenv("MAESTRO_HOME", str(home))
    return home
