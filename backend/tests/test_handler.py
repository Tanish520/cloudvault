import json
import logging

import pytest
from botocore.exceptions import ClientError

import files
import handler
from auth import AuthenticationError
from rate_limit import RateLimitError
from responses import response
from validation import ValidationError


def body(result):
    return json.loads(result["body"])


def assert_cors(result):
    assert result["headers"]["Access-Control-Allow-Origin"] == "*"


def test_root_logger_is_configured_at_info():
    assert logging.getLogger().level == logging.INFO


def test_routes_contain_exact_api_gateway_v2_route_keys():
    assert set(handler.ROUTES) == {
        "POST /files/upload-url",
        "POST /files/confirm",
        "GET /files",
        "GET /files/{fileId}/download",
        "DELETE /files/{fileId}",
    }


def test_unknown_route_returns_404_with_exact_message():
    result = handler.lambda_handler({"routeKey": "GET /unknown"}, None)

    assert result["statusCode"] == 404
    assert body(result) == {"message": "Route not found"}
    assert_cors(result)


def test_missing_route_returns_404():
    result = handler.lambda_handler({}, None)

    assert result["statusCode"] == 404
    assert body(result) == {"message": "Route not found"}


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        "not an event",
        {"routeKey": []},
        {"routeKey": {}},
    ],
)
def test_malformed_event_returns_route_not_found(event):
    result = handler.lambda_handler(event, None)

    assert result["statusCode"] == 404
    assert body(result) == {"message": "Route not found"}
    assert_cors(result)


def test_list_route_passes_event_to_list_files(monkeypatch):
    called = []

    def fake_list_files(event):
        called.append(event)
        return response(200, {"files": []})

    monkeypatch.setattr(files, "list_files", fake_list_files)

    event = {"routeKey": "GET /files"}
    result = handler.lambda_handler(event, None)

    assert called == [event]
    assert result["statusCode"] == 200
    assert body(result) == {"files": []}
    assert_cors(result)


def test_upload_route_passes_event(monkeypatch):
    event = {"routeKey": "POST /files/upload-url", "body": "payload"}
    monkeypatch.setattr(
        files,
        "create_upload_url",
        lambda received: response(200, {"event": received}),
    )

    result = handler.lambda_handler(event, None)

    assert result["statusCode"] == 200
    assert body(result) == {"event": event}


@pytest.mark.parametrize(
    ("route_key", "function_name"),
    [
        ("POST /files/confirm", "confirm_upload"),
        ("GET /files/{fileId}/download", "create_download_url"),
        ("DELETE /files/{fileId}", "delete_file"),
    ],
)
def test_event_routes_dispatch_to_expected_file_operation(
    monkeypatch, route_key, function_name
):
    event = {"routeKey": route_key, "marker": function_name}
    calls = []

    def operation(received):
        calls.append(received)
        return response(200, {"operation": function_name})

    monkeypatch.setattr(files, function_name, operation)

    result = handler.lambda_handler(event, None)

    assert calls == [event]
    assert body(result) == {"operation": function_name}


def test_validation_error_returns_400_with_exact_message(monkeypatch):
    def reject(_event):
        raise ValidationError("fileName is invalid")

    monkeypatch.setattr(files, "create_upload_url", reject)

    result = handler.lambda_handler(
        {"routeKey": "POST /files/upload-url"}, None
    )

    assert result["statusCode"] == 400
    assert body(result) == {"message": "fileName is invalid"}
    assert_cors(result)


def test_authentication_error_returns_401(monkeypatch):
    def unauthenticated(_event):
        raise AuthenticationError("Authentication required")

    monkeypatch.setattr(files, "create_upload_url", unauthenticated)

    result = handler.lambda_handler(
        {"routeKey": "POST /files/upload-url"}, None
    )

    assert result["statusCode"] == 401
    assert body(result) == {"message": "Authentication required"}
    assert_cors(result)


def test_rate_limit_error_returns_429_with_exact_message(monkeypatch):
    def limited(_event):
        raise RateLimitError("Too many requests. Please slow down.")

    monkeypatch.setattr(files, "list_files", limited)

    result = handler.lambda_handler({"routeKey": "GET /files"}, None)

    assert result["statusCode"] == 429
    assert body(result) == {"message": "Too many requests. Please slow down."}
    assert_cors(result)


def test_not_found_error_returns_404_with_exact_message(monkeypatch):
    def missing(_event):
        raise files.NotFoundError("File not found")

    monkeypatch.setattr(files, "create_download_url", missing)

    result = handler.lambda_handler(
        {"routeKey": "GET /files/{fileId}/download"}, None
    )

    assert result["statusCode"] == 404
    assert body(result) == {"message": "File not found"}


def test_conflict_error_returns_409_with_exact_message(monkeypatch):
    def conflict(_event):
        raise files.ConflictError("File already confirmed")

    monkeypatch.setattr(files, "confirm_upload", conflict)

    result = handler.lambda_handler({"routeKey": "POST /files/confirm"}, None)

    assert result["statusCode"] == 409
    assert body(result) == {"message": "File already confirmed"}


def test_client_error_returns_generic_500_without_aws_detail(monkeypatch):
    def aws_failure(_event):
        raise ClientError(
            {
                "Error": {
                    "Code": "AccessDenied",
                    "Message": "secret AWS detail",
                }
            },
            "DeleteObject",
        )

    monkeypatch.setattr(files, "delete_file", aws_failure)

    result = handler.lambda_handler(
        {"routeKey": "DELETE /files/{fileId}"}, None
    )

    assert result["statusCode"] == 500
    assert body(result) == {"message": "AWS operation failed"}
    assert "secret AWS detail" not in result["body"]
    assert_cors(result)


def test_unexpected_error_returns_generic_500(monkeypatch):
    def fail(_event):
        raise RuntimeError("sensitive implementation detail")

    monkeypatch.setattr(files, "list_files", fail)

    result = handler.lambda_handler({"routeKey": "GET /files"}, None)

    assert result["statusCode"] == 500
    assert body(result) == {"message": "Internal server error"}
    assert "sensitive implementation detail" not in result["body"]
