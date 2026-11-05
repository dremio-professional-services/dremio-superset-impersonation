"""Superset configuration for the local test environment only."""

import os

from dremio_impersonation import dremio_impersonation_mutator

SECRET_KEY = os.environ["SUPERSET_SECRET_KEY"]
SQLALCHEMY_DATABASE_URI = "sqlite:////app/superset_home/superset.db"
WTF_CSRF_ENABLED = False  # test API access with JWT only

DB_CONNECTION_MUTATOR = dremio_impersonation_mutator

# Chart/data cache: add the username to every cache key. Required because
# impersonate_user stays off on the database (see README).
FEATURE_FLAGS = {"CACHE_QUERY_BY_USER": True}

# File cache shared by all gunicorn worker processes in the container.
CACHE_CONFIG = {
    "CACHE_TYPE": "FileSystemCache",
    "CACHE_DIR": "/app/superset_home/cache",
    "CACHE_DEFAULT_TIMEOUT": 600,
}
DATA_CACHE_CONFIG = {**CACHE_CONFIG, "CACHE_DIR": "/app/superset_home/data_cache"}
