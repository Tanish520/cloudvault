"""Authentication context extracted from API Gateway's verified JWT."""


class AuthenticationError(Exception):
    """The Lambda invocation has no usable authenticated subject."""


def authenticated_user_id(event: dict) -> str:
    if not isinstance(event, dict):
        raise AuthenticationError("Authentication required")

    try:
        user_id = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        raise AuthenticationError("Authentication required") from None

    if not isinstance(user_id, str) or not user_id:
        raise AuthenticationError("Authentication required")

    return user_id
