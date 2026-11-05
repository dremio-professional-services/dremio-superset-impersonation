# Superset with the mutator installed from the wheel that `make build` put into
# dist/, for the local test environment. Tested versions and why they are
# chosen: README.md, "Requirements".
FROM apache/superset:6.1.0
USER root
COPY dist/*.whl /tmp/dist/
# The wheel brings its pinned sqlalchemy-dremio; pytest and coverage are for
# the mutator tests.
RUN uv pip install --python /app/.venv/bin/python --no-cache \
        /tmp/dist/*.whl pytest==8.4.2 coverage==7.11.0 \
    && uv pip check --python /app/.venv/bin/python \
    && rm -rf /tmp/dist
COPY local-test/superset/superset_config.py /app/pythonpath/superset_config.py
USER superset
