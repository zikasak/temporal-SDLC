"""Never send test payloads to a developer's configured Langfuse project."""
import os

# Set before application imports/.env loading. Tracing transport tests opt in
# explicitly in isolated child processes with disposable local credentials.
os.environ['LANGFUSE_TRACING_ENABLED'] = 'false'
