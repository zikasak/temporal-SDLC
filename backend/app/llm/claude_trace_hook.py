"""Run the pinned upstream plugin with the application's exporter and privacy policy."""
import importlib.util
import os
import sys
from pathlib import Path

from langfuse import propagate_attributes

from app.tracing import tracing_client


def main() -> int:
    if os.getenv('APP_CLAUDE_TRACING_ENABLED') != 'true':
        return 0
    client = tracing_client()
    if client is None:
        return 0
    source = Path(__file__).with_name('plugins') / 'langfuse-observability/hooks/langfuse_hook.py'
    spec = importlib.util.spec_from_file_location('langfuse_claude_plugin', source)
    plugin = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = plugin
    spec.loader.exec_module(plugin)
    plugin.create_langfuse_client = lambda config: client
    # Attached-mode observations must keep the app's session and feature instead
    # of assigning the native CLI's session ID as the Langfuse session.
    feature = os.getenv('APP_CLAUDE_TRACE_FEATURE')
    with propagate_attributes(session_id=os.getenv('APP_CLAUDE_TRACE_SESSION_ID') or None,
                              tags=[feature] if feature else None):
        return plugin.main()


if __name__ == '__main__':
    raise SystemExit(main())
