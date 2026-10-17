"""Environment-driven settings shared by the API and the worker."""

import os
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

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "ddalcu/Qwen3.8-27B-MLX-Serve-4bit")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# The SDK appends /chat/completions to this API base URL.
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "http://localhost:1234/v1")
# "pi" routes model calls through the Pi CLI instead of the provider above; Pi then
# uses its own credentials and, unless PI_MODEL names one, its default model.
LLM_HARNESS = os.getenv("LLM_HARNESS", "").lower()
PI_MODEL = os.getenv("PI_MODEL") or None
# "claude" routes them through Claude Code (the Claude Agent SDK) instead, with Claude
# Code's credentials and, unless CLAUDE_MODEL names one, its default model.
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or None
# Pi configuration directory: host Pi calls run against it, and its auth.json (and
# models.json, when present) is mounted into coding-agent containers. Pi's own
# PI_CODING_AGENT_DIR is honoured when this is unset.
PI_CREDENTIALS_DIR = Path(
    os.getenv("PI_CREDENTIALS_DIR") or os.getenv("PI_CODING_AGENT_DIR") or "~/.pi/agent"
).expanduser()

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

# Signs session tokens: at least 32 random characters, the same for every API replica.
# Changing it signs everyone out.
AUTH_JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "")
AUTH_TOKEN_TTL_HOURS = float(os.getenv("AUTH_TOKEN_TTL_HOURS", "12"))
# Sign-in attempts allowed per client address per minute, successful or not.
AUTH_LOGIN_ATTEMPTS_PER_MINUTE = int(os.getenv("AUTH_LOGIN_ATTEMPTS_PER_MINUTE", "10"))
# Reverse proxies between the client and the API that append to X-Forwarded-For. With 0
# the header is ignored and the connecting address is the client.
AUTH_TRUSTED_PROXY_HOPS = int(os.getenv("AUTH_TRUSTED_PROXY_HOPS", "0"))
