#!/bin/sh
set -eu
cd /certificates
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 -subj /CN=e2e-ca \
    -keyout ca.key -out ca.pem 2>/dev/null
for server in example.org example.net; do
    openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$server" \
        -keyout "$server.key" -out "$server.csr" 2>/dev/null
    printf 'subjectAltName=DNS:%s\nextendedKeyUsage=serverAuth\nbasicConstraints=CA:FALSE\n' "$server" \
        >"$server.ext"
    openssl x509 -req -in "$server.csr" -CA ca.pem -CAkey ca.key -CAcreateserial -days 1 \
        -extfile "$server.ext" -out "$server.pem" 2>/dev/null
done
chmod a+r ./*
