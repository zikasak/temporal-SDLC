# Langfuse Claude Observability Plugin

Vendored from https://github.com/langfuse/Claude-Observability-Plugin at
`b1619f994240ad49de4a97caa01d7d7853944fc0` (v1.2.2), under the accompanying MIT license.

`hooks/langfuse_hook.py` and `.claude-plugin/plugin.json` are unmodified upstream files.
`hooks/hooks.json` invokes `app.llm.claude_trace_hook`, which supplies the application's
Langfuse 4.16.0 client, content masking, private exporter and session attributes. This
avoids resolving dependencies at runtime and bypassing the application's privacy policy.

Update the pinned source and validate real SDK/MCP sessions and OTLP exports together.
