"""Fixtures for the mutator tests.

The Superset app is created when pytest loads this file, before it imports the
test modules: importing ``superset.models`` requires an initialized app.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from superset.app import create_app

if TYPE_CHECKING:
    from flask import Flask

APP = create_app()


@pytest.fixture(scope="session")
def app() -> Flask:
    """Return the real Superset app with the local test configuration."""
    return APP
