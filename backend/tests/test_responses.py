import json
from decimal import Decimal

import pytest

from responses import decimal_serializer, response


def test_response_formats_api_gateway_v2_proxy_response():
    result = response(200, {"message": "ok"})

    assert result["statusCode"] == 200
    assert result["headers"]["Content-Type"] == "application/json"
    assert result["headers"]["Access-Control-Allow-Origin"] == "*"
    assert json.loads(result["body"]) == {"message": "ok"}


def test_decimal_serializer_returns_int_for_whole_decimal():
    result = decimal_serializer(Decimal("245781"))

    assert result == 245781
    assert isinstance(result, int)


def test_decimal_serializer_returns_float_for_fractional_decimal():
    result = decimal_serializer(Decimal("1.5"))

    assert result == 1.5
    assert isinstance(result, float)


def test_response_serializes_list_body():
    body = [{"size": Decimal("2")}, {"size": Decimal("1.5")}]

    assert json.loads(response(200, body)["body"]) == [
        {"size": 2},
        {"size": 1.5},
    ]


def test_decimal_serializer_rejects_unsupported_type_with_type_name():
    with pytest.raises(TypeError, match="^Cannot serialize type: object$"):
        decimal_serializer(object())


def test_response_headers_are_not_shared_between_calls():
    first = response(200, {})
    first["headers"]["X-Test"] = "mutated"

    second = response(200, {})

    assert "X-Test" not in second["headers"]
