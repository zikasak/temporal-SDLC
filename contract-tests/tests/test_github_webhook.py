"""GitHub webhook deliveries and the webhook secret of the GitHub connection (FR-43, FR-44,
FR-49, FR-50, NFR-2), as far as the service answers without GitHub behind it: the suite
never connects an app, so the connection has no webhook secret and every delivery must be
rejected."""
import hashlib
import hmac
import json
import uuid

import pytest

EVENTS_PATH = "/connectors/github/events"


def mention_payload() -> dict:
    """A well-formed comment delivery mentioning an app, as GitHub would send it."""
    return {
        "action": "created",
        "comment": {"id": 1001, "body": "@some-app please fix the typo",
                    "html_url": "https://github.com/owner/repo/pull/7#issuecomment-1001",
                    "user": {"login": "maintainer"}},
        "issue": {"number": 7, "pull_request": {"url": "https://api.github.com/x"}},
        "repository": {"full_name": "owner/repo"},
        "sender": {"login": "maintainer"},
    }


def sign(body: bytes, secret: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def deliver(client, event: str = "issue_comment", signature: str | None = None,
            payload: dict | None = None):
    body = json.dumps(payload or mention_payload()).encode()
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event,
               "X-GitHub-Delivery": str(uuid.uuid4())}
    if signature == "sign-with-random-secret":
        signature = sign(body, uuid.uuid4().hex.encode())
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(EVENTS_PATH, content=body, headers=headers)


@pytest.mark.parametrize("signature", (
    None,
    "",
    "sha256=",
    "sha256=not-hex",
    "0" * 64,
    "sha256=" + "0" * 64,
    "sign-with-random-secret",
), ids=("unsigned", "empty", "no-digest", "not-hex", "no-prefix", "zero-digest",
        "signed-with-another-secret"))
def test_delivery_without_a_valid_signature_is_rejected(anonymous, signature):
    response = deliver(anonymous, signature=signature)
    assert response.status_code == 401, response.text
    assert isinstance(response.json()["detail"], str) and response.json()["detail"]


@pytest.mark.parametrize("event", ("issue_comment", "pull_request_review_comment", "ping"))
def test_every_event_type_is_rejected_while_the_connection_has_no_webhook_secret(
        anonymous, event):
    # Even a digest computed with an empty key or a guessable key must not be accepted
    # when no webhook secret is set (FR-49).
    body = json.dumps(mention_payload()).encode()
    for secret in (b"", b"secret", b"webhook-secret"):
        response = anonymous.post(EVENTS_PATH, content=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": event,
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign(body, secret)})
        assert response.status_code == 401, (event, secret, response.text)


def test_a_user_token_does_not_replace_the_webhook_signature(admin):
    response = deliver(admin)
    assert response.status_code == 401, response.text
    assert isinstance(response.json()["detail"], str)


def test_rejected_deliveries_do_not_use_up_their_delivery_id(anonymous):
    # A delivery that was not accepted is not "already handled": repeating the same
    # X-GitHub-Delivery answers 401 again, never 409.
    delivery_id = str(uuid.uuid4())
    body = json.dumps(mention_payload()).encode()
    for _ in range(2):
        response = anonymous.post(EVENTS_PATH, content=body, headers={
            "Content-Type": "application/json", "X-GitHub-Event": "issue_comment",
            "X-GitHub-Delivery": delivery_id})
        assert response.status_code == 401, response.text


def test_connection_status_reports_no_webhook_secret(admin):
    connection = admin.get("/connectors/github/connection").json()
    assert connection["configured"] is False
    assert connection["webhook_secret_set"] is False
    assert isinstance(connection["encryption_ready"], bool)
    assert "webhook_events_missing" in connection
    assert connection["webhook_events_missing"] is False
    assert "webhook_secret" not in connection


def test_failed_connect_with_a_webhook_secret_neither_echoes_nor_keeps_it(admin):
    secret = f"whsec-{uuid.uuid4().hex}"
    key = "-----BEGIN PRIVATE KEY-----\nnot-a-key\n-----END PRIVATE KEY-----"
    response = admin.put("/connectors/github/connection", json={
        "app_id": 12345, "private_key": key, "webhook_secret": secret})
    assert response.status_code in (422, 503), response.text
    assert secret not in response.text
    status = admin.get("/connectors/github/connection")
    assert status.json()["configured"] is False
    assert status.json()["webhook_secret_set"] is False
    assert secret not in status.text


@pytest.mark.parametrize("body", (
    {"app_id": 1, "private_key": "k", "webhook_secret": 12345},
    {"app_id": 1, "private_key": "k", "account": ["owner"]},
    {"private_key": "k"},
    {"app_id": None, "private_key": "k"},
), ids=("secret-not-a-string", "account-not-a-string", "no-app-id", "null-app-id"))
def test_connect_input_types_are_validated(admin, body):
    response = admin.put("/connectors/github/connection", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]


def test_manifest_code_must_be_a_string(admin):
    response = admin.post("/connectors/github/connection/manifest", json={"code": 12345})
    assert response.status_code == 422
