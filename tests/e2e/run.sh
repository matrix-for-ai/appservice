#!/bin/sh
set -eu

compose_image=docker.io/library/docker@sha256:5a7af0dcb0e46c803efe9b45371dd21cb500e5b4453250394ac6f06d49b6e6f1

server="${1:?usage: run.sh SERVER, one of: $(ls "$(dirname "$0")/servers")}"
repository="$(cd "$(dirname "$0")/../.." && pwd)"
server_directory="$repository/tests/e2e/servers/$server"
[ -d "$server_directory" ] || { echo "no such server: $server" >&2; exit 2; }
project="matrix-for-ai-e2e-$server-$(od -An -N4 -tx1 /dev/urandom | tr -d ' \n')"

engine="${CONTAINER_ENGINE:-docker}"
case "$engine" in
    docker) socket=/var/run/docker.sock; engine_options=
            up_options="--wait --wait-timeout 120" ;;
    podman) socket="${XDG_RUNTIME_DIR:?}/podman/podman.sock"
            engine_options="--security-opt label=disable -e DOCKER_BUILDKIT=0 -e COMPOSE_BAKE=false"
            up_options=--detach ;;
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
compose up --build $up_options $(compose config --services | grep -vx e2e) || status=$?
[ "$status" -ne 0 ] || compose run --build --no-deps --rm e2e || status=$?
if [ "$status" -eq 0 ]; then
    restart() { compose run --no-deps --rm -T e2e python tests/e2e/restart.py "$1"; }
    state=$(restart before) && compose stop appservice \
        && echo "$state" | restart while_down && compose start appservice \
        && echo "$state" | restart after && echo "restart: passed" || status=$?
fi
[ "$status" -eq 0 ] || compose logs --no-color
exit "$status"
