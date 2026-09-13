"""Lambda entry point and API Gateway route dispatch."""

import logging

from botocore.exceptions import ClientError

import files
from auth import AuthenticationError
from rate_limit import RateLimitError
from responses import response
from validation import ValidationError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ROUTES = {
    "POST /files/upload-url": lambda event: files.create_upload_url(event),
    "POST /files/confirm": lambda event: files.confirm_upload(event),
    "GET /files": lambda event: files.list_files(event),
    "GET /files/{fileId}/download": lambda event: files.create_download_url(
        event
    ),
    "DELETE /files/{fileId}": lambda event: files.delete_file(event),
}


def lambda_handler(event: dict, context) -> dict:
    route_key = event.get("routeKey") if isinstance(event, dict) else None
    route = ROUTES.get(route_key) if isinstance(route_key, str) else None

    if route is None:
        return response(404, {"message": "Route not found"})

    try:
        return route(event)
    except AuthenticationError as exc:
        return response(401, {"message": str(exc)})
    except RateLimitError as exc:
        return response(429, {"message": str(exc)})
    except ValidationError as exc:
        return response(400, {"message": str(exc)})
    except files.NotFoundError as exc:
        return response(404, {"message": str(exc)})
    except files.ConflictError as exc:
        return response(409, {"message": str(exc)})
    except ClientError:
        logger.exception("AWS operation failed route=%s", route_key)
        return response(500, {"message": "AWS operation failed"})
    except Exception:
        logger.exception("Unexpected error route=%s", route_key)
        return response(500, {"message": "Internal server error"})
