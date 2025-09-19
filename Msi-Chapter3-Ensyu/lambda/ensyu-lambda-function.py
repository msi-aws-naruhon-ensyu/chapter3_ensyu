import json
import os
import logging
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# --- Configuration & Clients -------------------------------------------------
# Default table name is "Items"; can be overridden by env var TABLE_NAME.
TABLE_NAME = os.getenv("TABLE_NAME", "Items")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(TABLE_NAME)

# Configure logging once per execution environment
logger = logging.getLogger(__name__)
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

# CORS headers reused for all responses
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Content-Type": "application/json",
}

# --- Helpers -----------------------------------------------------------------
def _response(status: int, body: Any = "") -> Dict[str, Any]:
    """Build an API Gateway compatible response with common CORS headers."""
    if isinstance(body, (dict, list)):
        body = json.dumps(body, ensure_ascii=False)
    elif body is None:
        body = ""
    return {"statusCode": status, "headers": CORS_HEADERS, "body": body}

def _parse_event(event: Dict[str, Any]) -> Tuple[str, str, Optional[str], Dict[str, Any]]:
    """Extract method, path, id path parameter, and JSON body from the event.

    Supports both REST and HTTP APIs (v1/v2). Path handling accepts `/Items`
    and also tolerates lowercase `/items` for safety.
    """
    # HTTP method (REST: httpMethod, HTTP API v2: requestContext.http.method)
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or ""
    method = method.upper()

    # Path (REST: path, HTTP API v2: rawPath)
    path = event.get("path") or event.get("rawPath") or "/"

    # Path parameter "id" (if provided as /Items/{id} or in event.pathParameters.id)
    path_params = event.get("pathParameters") or {}
    item_id = path_params.get("id")
    if not item_id:
        # Fallback: parse from path like /Items/{id or nested}
        segments = [s for s in path.split("/") if s]
        if len(segments) >= 2 and segments[0].lower() == "items":
            item_id = "/".join(segments[1:])  # allow nested ids if needed

    # Body (JSON or empty object)
    raw_body = event.get("body") or "{}"
    try:
        body = json.loads(raw_body) if isinstance(raw_body, str) else (raw_body or {})
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON in request body")

    return method, path, item_id, body

def _validate_item_payload(payload: Dict[str, Any]) -> Optional[str]:
    """Validate presence of required fields for POST and PUT operations."""
    required = ("id", "description", "date")
    missing = [k for k in required if k not in payload]
    if missing:
        return f"必須項目不足: {', '.join(missing)}"
    for k in required:
        if not isinstance(payload[k], str):
            return f"'{k}' は文字列である必要があります"
    return None

# --- Lambda Handler ----------------------------------------------------------
def lambda_handler(event, context):
    try:
        method, path, item_id, body = _parse_event(event)
        logger.info("Request: method=%s path=%s id=%s body_keys=%s", method, path, item_id, list(body.keys()))

        if method == "OPTIONS":
            # For REST APIs when OPTIONS is integrated with Lambda.
            # (HTTP API with CORS enabled auto-responds and won't hit Lambda)
            return _response(204, "")

        if method == "GET":
            if item_id:
                try:
                    resp = table.get_item(Key={"id": item_id})
                    item = resp.get("Item")
                    if not item:
                        return _response(404, {"message": "not found"})
                    return _response(200, item)
                except (ClientError, BotoCoreError):
                    logger.exception("GET get_item failed")
                    return _response(500, {"message": "internal error"})
            else:
                try:
                    # Simple scan for demo; in production consider pagination/Query.
                    resp = table.scan()
                    items = resp.get("Items", [])
                    return _response(200, items)
                except (ClientError, BotoCoreError):
                    logger.exception("GET scan failed")
                    return _response(500, {"message": "internal error"})

        if method == "POST":
            if item_id:
                # POST to /Items should not include id in path
                return _response(400, {"message": "POST ではパスに id を含めないでください"})
            err = _validate_item_payload(body)
            if err:
                return _response(400, {"message": err})
            try:
                table.put_item(Item=body)
                return _response(201, {"message": "created"})
            except (ClientError, BotoCoreError):
                logger.exception("POST put_item failed")
                return _response(500, {"message": "internal error"})

        if method == "PUT":
            if not item_id:
                return _response(400, {"message": "id が必要です"})
            try:
                table.update_item(
                    Key={"id": item_id},
                    UpdateExpression="SET #d = :d, #t = :t",
                    ExpressionAttributeNames={"#d": "description", "#t": "date"},
                    ExpressionAttributeValues={
                        ":d": body.get("description"),
                        ":t": body.get("date"),
                    },
                )
                return _response(200, {"message": "updated"})
            except (ClientError, BotoCoreError):
                logger.exception("PUT update_item failed")
                return _response(500, {"message": "internal error"})

        if method == "DELETE":
            del_id = item_id or body.get("id")
            if not del_id:
                return _response(400, {"message": "id 必須"})
            try:
                table.delete_item(Key={"id": del_id})
                return _response(200, {"message": "deleted"})
            except (ClientError, BotoCoreError):
                logger.exception("DELETE delete_item failed")
                return _response(500, {"message": "internal error"})

        return _response(405, {"message": "unsupported"})

    except ValueError as ve:
        logger.warning("Bad request: %s", ve)
        return _response(400, {"message": str(ve)})
    except Exception:
        logger.exception("Unhandled error")
        return _response(500, {"message": "internal error"})
