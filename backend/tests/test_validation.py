import base64
import json

import pytest

from validation import (
    ALLOWED_CONTENT_TYPES,
    MAX_FILE_SIZE,
    ValidationError,
    parse_body,
    require_fields,
    sanitize_filename,
    validate_content_type,
    validate_size,
)


def test_parse_body_reads_plain_json():
    assert parse_body({"body": '{"a": 1}'}) == {"a": 1}


def test_parse_body_returns_empty_dict_when_absent():
    assert parse_body({}) == {}
    assert parse_body({"body": ""}) == {}


def test_parse_body_decodes_base64():
    encoded = base64.b64encode(json.dumps({"a": 1}).encode()).decode()
    event = {"body": encoded, "isBase64Encoded": True}
    assert parse_body(event) == {"a": 1}

    with pytest.raises(
        ValidationError, match="^Request body is not valid base64 UTF-8$"
    ):
        parse_body({"body": "not-base64!", "isBase64Encoded": True})


def test_parse_body_rejects_malformed_json():
    with pytest.raises(
        ValidationError, match="^Request body must contain valid JSON$"
    ):
        parse_body({"body": "{not json"})


def test_parse_body_rejects_non_object_json():
    with pytest.raises(ValidationError, match="^Request body must be a JSON object$"):
        parse_body({"body": "[1, 2, 3]"})


def test_sanitize_filename_strips_directory_traversal():
    assert sanitize_filename("../../etc/passwd") == "passwd"


def test_sanitize_filename_replaces_unsafe_characters():
    assert sanitize_filename("my report (final).pdf") == "my_report__final_.pdf"


def test_sanitize_filename_keeps_safe_characters():
    assert sanitize_filename("resume-v2.final.pdf") == "resume-v2.final.pdf"


def test_sanitize_filename_rejects_name_that_empties_out():
    for filename in ("..", "   "):
        with pytest.raises(ValidationError, match="^Invalid file name$"):
            sanitize_filename(filename)


def test_sanitize_filename_rejects_non_string():
    with pytest.raises(ValidationError, match="^fileName must be a string$"):
        sanitize_filename(None)


def test_validate_content_type_accepts_allowed():
    expected = frozenset(
        {"application/pdf", "image/png", "image/jpeg", "text/plain"}
    )
    assert ALLOWED_CONTENT_TYPES == expected
    for content_type in expected:
        assert validate_content_type(content_type) == content_type


def test_validate_content_type_rejects_others():
    with pytest.raises(ValidationError, match="^Unsupported file type$"):
        validate_content_type("application/x-msdownload")


@pytest.mark.parametrize("bad", [[], {}])
def test_validate_content_type_rejects_non_string(bad):
    with pytest.raises(ValidationError, match="^Unsupported file type$"):
        validate_content_type(bad)


def test_validate_size_accepts_valid():
    assert MAX_FILE_SIZE == 10 * 1024 * 1024
    assert validate_size(1024) == 1024
    assert validate_size(MAX_FILE_SIZE) == MAX_FILE_SIZE


@pytest.mark.parametrize("bad", [0, -1, "1024", 1.5, None, True, MAX_FILE_SIZE + 1])
def test_validate_size_rejects_invalid(bad):
    expected = (
        "Maximum file size is 10 MB"
        if bad == MAX_FILE_SIZE + 1
        else "Invalid file size"
    )
    with pytest.raises(ValidationError, match=f"^{expected}$"):
        validate_size(bad)


def test_require_fields_passes_when_present():
    require_fields({"a": 1, "b": 2}, ["a", "b"])


def test_require_fields_names_the_missing_ones():
    with pytest.raises(ValidationError) as excinfo:
        require_fields({"a": 1, "b": None}, ["a", "b", "c"])

    assert "b" in str(excinfo.value)
    assert "c" in str(excinfo.value)
