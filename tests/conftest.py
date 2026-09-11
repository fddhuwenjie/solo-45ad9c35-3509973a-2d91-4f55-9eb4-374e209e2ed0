"""Shared pytest fixtures: isolated DB and a TestClient."""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from app import runtime  # noqa: E402
from app.db import Store  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    runtime.set_store(s)
    yield s


@pytest.fixture()
def client(store):
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c
