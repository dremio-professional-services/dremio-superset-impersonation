#!/bin/sh
# Trust the local test CA in Chromium (NSS), then run pytest with the given
# arguments.
set -e
mkdir -p "$HOME/.pki/nssdb"
certutil -d "sql:$HOME/.pki/nssdb" -N --empty-password 2>/dev/null || true
certutil -d "sql:$HOME/.pki/nssdb" -A -t "C,," -n dremio-local-test-ca -i /run/dremio/ca.pem
exec python -m pytest -p no:cacheprovider "$@"
