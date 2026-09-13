import pytest

from auth import AuthenticationError, authenticated_user_id


def authenticated_event(sub="user-a-sub"):
    return {
        "requestContext": {
            "authorizer": {
                "jwt": {
                    "claims": {"sub": sub, "email": "user@example.com"}
                }
            }
        }
    }


def test_authenticated_user_id_returns_verified_sub():
    assert authenticated_user_id(authenticated_event()) == "user-a-sub"


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"requestContext": {}},
        {"requestContext": {"authorizer": {}}},
        {"requestContext": {"authorizer": {"jwt": {}}}},
        {"requestContext": {"authorizer": {"jwt": {"claims": {}}}}},
        authenticated_event(sub=""),
        authenticated_event(sub=123),
    ],
)
def test_authenticated_user_id_rejects_missing_or_invalid_sub(event):
    with pytest.raises(AuthenticationError, match="^Authentication required$"):
        authenticated_user_id(event)


def test_authenticated_user_id_ignores_untrusted_user_id_fields():
    event = authenticated_event()
    event["body"] = '{"userId":"attacker"}'
    event["queryStringParameters"] = {"userId": "attacker"}

    assert authenticated_user_id(event) == "user-a-sub"
