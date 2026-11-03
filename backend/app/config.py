"""Environment-driven settings shared by the API and the worker."""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

# Load local development settings before any provider or tracing initialization.
# Deployment environment variables always take precedence.
load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

APP_DATABASE_URL = os.getenv(
    "APP_DATABASE_URL", "postgresql://temporal:temporal@localhost:5433/temporal"
)
DOCUMENT_STORAGE_DIR = Path(
    os.getenv(
        "DOCUMENT_STORAGE_DIR",
        str(Path(__file__).resolve().parents[1] / "data" / "documents"),
    )
).resolve()

TEMPORAL_ADDRESS = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "my-task-queue")
CODEGEN_TASK_QUEUE = os.getenv("TEMPORAL_CODEGEN_TASK_QUEUE", "codegen")
IMPLEMENTATION_TASK_QUEUE = os.getenv("TEMPORAL_IMPLEMENTATION_TASK_QUEUE", "implementation")
TEST_GENERATION_TASK_QUEUE = os.getenv("TEMPORAL_TEST_GENERATION_TASK_QUEUE", "test-generation")
VERIFICATION_TASK_QUEUE = os.getenv("TEMPORAL_VERIFICATION_TASK_QUEUE", "verification")
DIAGNOSIS_TASK_QUEUE = os.getenv("TEMPORAL_DIAGNOSIS_TASK_QUEUE", "diagnosis")
DOCUMENTATION_TASK_QUEUE = os.getenv("TEMPORAL_DOCUMENTATION_TASK_QUEUE", "documentation")
REVIEW_TASK_QUEUE = os.getenv("TEMPORAL_REVIEW_TASK_QUEUE", "review")
ROUTING_TASK_QUEUE = os.getenv("TEMPORAL_ROUTING_TASK_QUEUE", "routing")

GENERATED_CODE_DIR = Path(
    os.getenv(
        "GENERATED_CODE_DIR",
        str(Path(__file__).resolve().parents[1] / "data" / "generated"),
    )
).resolve()
OPENAPI_GENERATOR_CMD = os.getenv(
    "OPENAPI_GENERATOR_CMD",
    "java -jar " + str(
        Path(__file__).resolve().parents[1] / "tools" / "openapi-generator-cli-7.22.0.jar"
    ),
)
CODEGEN_BASE_PACKAGE = os.getenv("CODEGEN_BASE_PACKAGE", "com.example")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# The SDK appends /chat/completions to this API base URL.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
# Adapter, model and thinking level are stored per workflow in app.workflow_settings.
# Pi configuration directory: host Pi calls run against it, and its auth.json (and
# models.json, when present) is mounted into coding-agent containers. Pi's own
# PI_CODING_AGENT_DIR is honoured when this is unset.
PI_CREDENTIALS_DIR = Path(
    os.getenv("PI_CREDENTIALS_DIR") or os.getenv("PI_CODING_AGENT_DIR") or "~/.pi/agent"
).expanduser()
# A dedicated credential directory mounted at the same path in workers and the
# sandbox daemon can be shared whole, preserving Pi's sibling token-refresh lock.
# Keep false for a developer's Pi home, which can also contain extensions/sessions.
PI_CREDENTIALS_SHARED = os.getenv('PI_CREDENTIALS_SHARED', '').lower() in {'1', 'true', 'yes'}

# Contracts larger than this are supplied to coding agents as an index plus read-only
# files instead of being embedded whole in every model invocation.
CONTRACT_EMBED_CHARS = int(os.getenv("CONTRACT_EMBED_CHARS", "32000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Shared GitHub connector: encrypts the saved GitHub App private key. Persist it
# separately from the database.
GITHUB_TOKEN_ENCRYPTION_KEY = os.getenv("GITHUB_TOKEN_ENCRYPTION_KEY", "")
GITHUB_ALLOWED_ORIGINS = frozenset(
    origin.strip() for origin in os.getenv(
        "GITHUB_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000",
    ).split(",") if origin.strip()
)

# The public address GitHub delivers webhook events to, such as
# https://app.example.com/api/connectors/github/events behind the web client. Without
# it, a connection check derives it from the request and its proxy headers.
GITHUB_WEBHOOK_URL = os.getenv("GITHUB_WEBHOOK_URL", "").strip()
if not re.fullmatch(r"https?://[^\s/?#]+(/[^\s?#]*)?", GITHUB_WEBHOOK_URL):
    # Not an http or https URL: derived from the request instead.
    GITHUB_WEBHOOK_URL = ""

# Signs session tokens: at least 32 random characters, the same for every API replica.
# Changing it signs everyone out.
AUTH_JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "")
AUTH_TOKEN_TTL_HOURS = float(os.getenv("AUTH_TOKEN_TTL_HOURS", "12"))
# Sign-in attempts allowed per client address per minute, successful or not.
AUTH_LOGIN_ATTEMPTS_PER_MINUTE = int(os.getenv("AUTH_LOGIN_ATTEMPTS_PER_MINUTE", "10"))
# Reverse proxies between the client and the API that append to X-Forwarded-For. With 0
# the header is ignored and the connecting address is the client.
AUTH_TRUSTED_PROXY_HOPS = int(os.getenv("AUTH_TRUSTED_PROXY_HOPS", "0"))
