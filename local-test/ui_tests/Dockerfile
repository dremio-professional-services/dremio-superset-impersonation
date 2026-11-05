# Browser tests against the running local stack (headless Chromium).
FROM mcr.microsoft.com/playwright/python:v1.64.1-noble
RUN apt-get update \
    && apt-get install -y --no-install-recommends libnss3-tools \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir --break-system-packages \
        playwright==1.64.1 pytest-playwright==0.10.0 pytest==8.4.2 requests==2.32.4
COPY entrypoint.sh /entrypoint.sh
WORKDIR /ui_tests
ENTRYPOINT ["/bin/sh", "/entrypoint.sh"]
