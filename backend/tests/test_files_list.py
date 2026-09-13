import json
from decimal import Decimal
from unittest.mock import Mock, call

import files
from boto3.dynamodb.conditions import Key
from conftest import USER_A, USER_B, with_auth
from files import list_files


def _body(result):
    return json.loads(result["body"])


def _item(user_id, file_id, uploaded_at, size=None):
    item = {
        "userId": user_id,
        "fileId": file_id,
        "uploadedAt": uploaded_at,
    }
    if size is not None:
        item["size"] = size
    return item


def test_list_files_returns_empty_list_for_authenticated_user(aws):
    assert _body(list_files(with_auth())) == {"files": []}


def test_list_files_returns_only_authenticated_users_items(aws):
    own = _item(USER_A, "own", "2026-02-01T00:00:00+00:00")
    other = _item(USER_B, "other", "2026-03-01T00:00:00+00:00")
    aws["table"].put_item(Item=own)
    aws["table"].put_item(Item=other)

    assert _body(list_files(with_auth(user_id=USER_A)))["files"] == [own]


def test_list_files_returns_newest_first_and_serializes_decimal(aws):
    older = _item(
        USER_A, "older", "2026-01-01T00:00:00+00:00", Decimal("42")
    )
    newer = _item(
        USER_A, "newer", "2026-02-01T00:00:00+00:00", Decimal("1.5")
    )
    aws["table"].put_item(Item=older)
    aws["table"].put_item(Item=newer)

    listed = _body(list_files(with_auth()))["files"]

    assert [item["fileId"] for item in listed] == ["newer", "older"]
    assert listed[0]["size"] == 1.5
    assert listed[1]["size"] == 42


def test_list_files_paginates_query_with_the_same_user_key(monkeypatch):
    page_key = {"userId": USER_A, "fileId": "page-one-last"}
    table = Mock()
    table.query.side_effect = [
        {
            "Items": [_item(USER_A, "oldest", "2026-01-01T00:00:00+00:00")],
            "LastEvaluatedKey": page_key,
        },
        {
            "Items": [_item(USER_A, "newest", "2026-03-01T00:00:00+00:00")]
        },
    ]
    monkeypatch.setattr(files, "_table", Mock(return_value=table))
    monkeypatch.setattr(files, "check_rate_limit", lambda user_id: None)

    result = _body(list_files(with_auth()))["files"]

    assert [item["fileId"] for item in result] == ["newest", "oldest"]
    assert table.query.call_args_list == [
        call(KeyConditionExpression=Key("userId").eq(USER_A)),
        call(
            KeyConditionExpression=Key("userId").eq(USER_A),
            ExclusiveStartKey=page_key,
        ),
    ]
    table.scan.assert_not_called()
