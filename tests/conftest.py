"""Shared test setup."""

import tempfile

import pytest


@pytest.fixture(autouse=True, scope="session")
def _private_temp_dir(tmp_path_factory):
    """Run the whole suite against a temp directory of its own.

    Starting the app sweeps stale LibreOffice profiles from the temp directory, and
    the real conversions write profiles and workspaces there. Neither may touch the
    machine's real /tmp, where a server running next to the tests keeps its own.
    """
    saved = tempfile.tempdir
    tempfile.tempdir = str(tmp_path_factory.mktemp("tmp"))
    yield
    tempfile.tempdir = saved
