import json
from fnmatch import fnmatch
from pathlib import Path

from aiohttp import web
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SPEC = json.loads(
    (Path(__file__).resolve().parent.parent / "matrix_for_ai" / "api" / "openapi.json").read_text())
METHODS = ("get", "post", "put", "patch", "delete")
_REGISTRY = Registry().with_resource(
    "urn:openapi", Resource(contents=SPEC, specification=DRAFT202012))


def operations() -> dict:
    return {(method, path): operation for path, item in SPEC["paths"].items()
            for method, operation in item.items() if method in METHODS}


def declared() -> set:
    outcomes = set()
    for key, operation in operations().items():
        for status, response in operation["responses"].items():
            if int(status) < 400:
                outcomes.add((*key, int(status), None))
            else:
                schema = response["content"]["application/problem+json"]["schema"]
                outcomes.update(
                    (*key, int(status), code) for code in schema["properties"]["code"]["enum"])
    return outcomes


def _pointer(*parts: str) -> str:
    return "#/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in parts)


def _errors(pointer: str, instance) -> list[str]:
    validator = Draft202012Validator({"$ref": "urn:openapi" + pointer}, registry=_REGISTRY)
    return [f"{'/'.join(map(str, error.absolute_path)) or '(root)'}: {error.message}"
            for error in validator.iter_errors(instance)]


class ContractRecorder:
    def __init__(self) -> None:
        self.seen: set = set()
        self.violations: list[str] = []
        self._operations = operations()

    @web.middleware
    async def middleware(self, request: web.Request, handler):
        response = await handler(request)
        resource = request.match_info.route.resource
        key = (request.method.lower(), resource.canonical if resource else None)
        if key in self._operations:
            await self._check(key, request, response)
        return response

    async def _check(self, key, request, response) -> None:
        method, path = key
        operation = self._operations[key]
        status = str(response.status)
        where = f"{method.upper()} {path} → {status}"
        body = None
        if response.body and response.content_type in ("application/json",
                                                       "application/problem+json"):
            body = json.loads(response.body)
        api_version = response.headers.get("X-API-Version", "")
        if not api_version.isdigit():
            self.violations.append(f"{where}: X-API-Version missing or not an integer")
        else:
            self.violations += [
                f"{where}: X-API-Version {error}" for error in
                _errors(_pointer("components", "headers", "X-API-Version", "schema"), int(api_version))]
        self.seen.add((method, path, response.status,
                       body.get("code") if response.status >= 400 and body else None))
        if status not in operation["responses"]:
            self.violations.append(f"{where}: status not documented"
                                   + (f" (code {body.get('code')})" if body else ""))
            return
        documented = operation["responses"][status].get("content", {})
        if not documented:
            if response.body:
                self.violations.append(f"{where}: documented without a body, answered one")
            return
        if not any(fnmatch(response.content_type, accepted) for accepted in documented):
            self.violations.append(f"{where}: content type {response.content_type} not documented")
            return
        if body is not None:
            pointer = _pointer("paths", path, method, "responses", status, "content",
                               response.content_type, "schema")
            self.violations += [f"{where}: {error}" for error in _errors(pointer, body)]
        if response.status < 400:
            await self._check_request(where, path, method, operation, request)

    async def _check_request(self, where, path, method, operation, request) -> None:
        accepted = {parameter["name"] for parameter in self._parameters(path, operation)
                    if parameter["in"] == "query"}
        for name in set(request.query) - accepted:
            self.violations.append(f"{where}: accepted query parameter {name} not documented")
        documented = operation.get("requestBody", {}).get("content", {})
        if not request.body_exists:
            return
        if request.content_type not in documented:
            self.violations.append(f"{where}: accepted body of type {request.content_type} "
                                   "not documented")
            return
        if not request.content_type.endswith("json"):
            return
        pointer = _pointer("paths", path, method, "requestBody", "content", request.content_type,
                           "schema")
        payload = json.loads(await request.read())
        self.violations += [f"{where}: request {error}" for error in _errors(pointer, payload)]

    @staticmethod
    def _parameters(path, operation) -> list[dict]:
        parameters = SPEC["paths"][path].get("parameters", []) + operation.get("parameters", [])
        resolved = []
        for parameter in parameters:
            if "$ref" in parameter:
                parameter = SPEC["components"]["parameters"][parameter["$ref"].rsplit("/", 1)[1]]
            resolved.append(parameter)
        return resolved


RECORDER = ContractRecorder()
