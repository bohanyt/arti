"""Shared, connection-local VTS auth and identity checks (no runtime owners)."""
from pathlib import Path


class VTSAPIError(RuntimeError):
    def __init__(self, error_id):
        self.error_id = error_id
        # Do not echo arbitrary API payloads: auth replies can contain tokens.
        super().__init__(f"VTS APIError errorID={error_id}")


def response_data(response):
    data = response.get("data") or {}
    if response.get("messageType") == "APIError" or "errorID" in data:
        raise VTSAPIError(data.get("errorID"))
    return data


def token_path(script_file, port):
    return Path(script_file).resolve().parent / f"vts_token_{int(port)}.txt"


def read_token(script_file, port, *, main_port):
    path = token_path(script_file, port)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    # Legacy belongs only to the configured main port, never a second instance.
    legacy = path.with_name("vts_token.txt")
    if int(port) == int(main_port) and legacy.exists():
        return legacy.read_text(encoding="utf-8").strip()
    return None


def model_matches(response, expected):
    data = response_data(response)
    return data.get("modelLoaded") is True and data.get("modelName") == expected
