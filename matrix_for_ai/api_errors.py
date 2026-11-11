from http import HTTPStatus

PROBLEM_CONTENT_TYPE = "application/problem+json"


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail: str, *, headers: dict | None = None,
                 **extensions) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail
        self.headers = headers or {}
        self.extensions = extensions

    def as_problem(self) -> dict:
        return problem(self.status, self.code, self.detail, **self.extensions)


def homeserver_problem(status: int | None, body=None) -> dict:
    if status is None:
        return problem(502, "homeserver_error", "the homeserver did not answer")
    upstream = {"status": status}
    errcode = body.get("errcode") if isinstance(body, dict) else None
    if isinstance(errcode, str):
        upstream["errcode"] = errcode
    return problem(502, "homeserver_error", f"the homeserver answered {status}",
                   upstream=upstream)


def problem(status: int, code: str, detail: str, **extensions) -> dict:
    return {"type": "about:blank", "title": HTTPStatus(status).phrase, "status": status,
            "detail": detail, "code": code, **extensions}
