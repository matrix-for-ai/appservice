#!/bin/sh
set -eu

compose_image=docker.io/library/docker@sha256:48678dab48932d2b9189040748f321d4eb09c90193bbf4f4b9b86d72cffd6e25

server="${1:?usage: run.sh SERVER, one of: $(ls "$(dirname "$0")/servers")}"
repository="$(cd "$(dirname "$0")/../.." && pwd)"
server_directory="$repository/tests/e2e/servers/$server"
[ -d "$server_directory" ] || { echo "no such server: $server" >&2; exit 2; }
project="matrix-for-ai-e2e-$server-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"

engine="${CONTAINER_ENGINE:-docker}"
case "$engine" in
    docker) socket=/var/run/docker.sock; engine_options= ;;
    podman) socket="${XDG_RUNTIME_DIR:?}/podman/podman.sock"
            engine_options="--security-opt label=disable -e DOCKER_BUILDKIT=0 -e COMPOSE_BAKE=false" ;;
    *) echo "CONTAINER_ENGINE must be docker or podman" >&2; exit 2 ;;
esac

compose() {
    "$engine" run --rm -i $engine_options -v "$socket:/var/run/docker.sock" \
        -v "$repository:$repository" -w "$repository" "$compose_image" \
        docker compose --project-name "$project" \
        --file "$server_directory/compose.yaml" --env-file "$server_directory/test.env" "$@"
}

trap 'compose down --volumes --timeout 5 >/dev/null 2>&1' EXIT
status=0
compose run --build --rm e2e || status=$?
[ "$status" -eq 0 ] || compose logs --no-color
exit "$status"
