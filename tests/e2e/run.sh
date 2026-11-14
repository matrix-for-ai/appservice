#!/bin/sh
set -eu

server="${1:?usage: run.sh SERVER, one of: $(ls "$(dirname "$0")/servers")}"
server_directory="$(cd "$(dirname "$0")/servers/$server" && pwd)"
project="matrix-for-ai-e2e-$server-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"

compose() {
    ${COMPOSE:-docker compose} --project-name "$project" \
        --file "$server_directory/compose.yaml" --env-file "$server_directory/test.env" "$@"
}

trap 'compose down --volumes --timeout 5 >/dev/null 2>&1' EXIT
status=0
compose run --build --rm e2e || status=$?
[ "$status" -eq 0 ] || compose logs --no-color
exit "$status"
