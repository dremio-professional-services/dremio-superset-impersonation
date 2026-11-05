#!/bin/sh
# Runs inside an alpine container: local test CA + Dremio server certificate.
set -eu
apk add --no-cache openssl >/dev/null
mkdir -p /state/dremio /state/superset && cd /state/dremio
[ -f dremio.p12 ] && [ -f /state/superset/ca.pem ] && exit 0
openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=dremio-local-test-ca" \
  -keyout ca.key -out ca.pem 2>/dev/null
openssl req -newkey rsa:2048 -nodes -subj "/CN=dremio" -keyout dremio.key -out dremio.csr 2>/dev/null
printf 'subjectAltName=DNS:dremio,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth\n' > ext.cnf
openssl x509 -req -in dremio.csr -CA ca.pem -CAkey ca.key -CAcreateserial -days 30 \
  -extfile ext.cnf -out dremio.crt 2>/dev/null
openssl pkcs12 -export -inkey dremio.key -in dremio.crt -certfile ca.pem \
  -passout pass:changeit -name dremio -out dremio.p12
rm -f dremio.csr ext.cnf ca.srl ca.key
chmod 644 ca.pem dremio.p12
cp ca.pem /state/superset/ca.pem
