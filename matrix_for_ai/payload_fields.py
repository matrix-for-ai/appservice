from .api_errors import ApiError


def reject_unknown_fields(payload: dict, allowed, *, section: str | None = None) -> None:
    unknown = sorted(set(payload) - set(allowed))
    if unknown:
        where = f" in {section}" if section else ""
        raise ApiError(422, "unknown_field",
                       f"unknown field{where}: {', '.join(unknown)} "
                       f"(accepted: {', '.join(sorted(allowed))})")


def optional_string(payload: dict, key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(422, f"invalid_{key}", f"{key} must be a string")
    return value.strip() or None


def required_string(payload: dict, key: str) -> str:
    value = optional_string(payload, key)
    if value is None:
        raise ApiError(422, f"{key}_required", f"{key} is required")
    return value


def optional_flag(payload: dict, key: str, *, default: bool) -> bool:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ApiError(422, f"invalid_{key}", f"{key} must be true or false")
    return value


def optional_positive_integer(payload: dict, key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApiError(422, f"invalid_{key}", f"{key} must be a positive integer")
    return value
