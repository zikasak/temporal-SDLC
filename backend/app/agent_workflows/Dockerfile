FROM maven:3.9.9-eclipse-temurin-21 AS java
FROM node:24-slim AS node
FROM python:3.12-slim
# Git: repositories' own tests and checks scripts may call it, and so may the agents.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=java /opt/java/openjdk /opt/java/openjdk
COPY --from=java /usr/share/maven /usr/share/maven
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:/usr/share/maven/bin:${PATH}"
# Node and the Pi CLI serve the pi and openai adapters; Pi's credentials are mounted at
# run time. The Langfuse plugin traces its sessions (app/llm/pi.py).
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global @earendil-works/pi-coding-agent@1.6.0 \
        @langfuse/pi-observability-plugin@0.1.2 && npm cache clean --force
ENV PI_LANGFUSE_PLUGIN=/usr/local/lib/node_modules/@langfuse/pi-observability-plugin
RUN pip install --no-cache-dir langchain==1.4.2 langfuse==4.16.0
# The Claude Agent SDK bundles the Claude Code CLI that serves the Claude adapter; its
# credentials arrive as environment variables at run time. Its MCP package serves the
# flow's tools to Pi as well.
RUN pip install --no-cache-dir claude-agent-sdk==0.2.186
# Static tools the host runs over a delivery before its review, without a network
# (app/languages/static_checks.py): a secrets scanner and Python's linter.
RUN arch="$(uname -m | sed 's/x86_64/x64/; s/aarch64/arm64/')" \
    && python -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/tmp/gitleaks.tgz')" \
        "https://github.com/gitleaks/gitleaks/releases/download/v8.21.2/gitleaks_8.21.2_linux_${arch}.tar.gz" \
    && tar -xzf /tmp/gitleaks.tgz -C /usr/local/bin gitleaks && rm /tmp/gitleaks.tgz \
    && gitleaks version
RUN pip install --no-cache-dir ruff==0.8.4
ARG AGENT_ROLE
ARG WORKSPACE_NAME
COPY app/__init__.py app/asyncio_utils.py app/tracing.py /opt/app/
COPY app/agent_workflows/__init__.py app/agent_workflows/runtime.py /opt/app/agent_workflows/
COPY app/llm/__init__.py app/llm/factory.py app/llm/native.py app/llm/native_sessions.py app/llm/saved_conversations.py app/llm/claude.py app/llm/mcp.py app/llm/claude_trace_hook.py app/llm/pi.py app/llm/pi_extension.ts app/llm/pi_trace.py app/llm/questions.py app/llm/limits.py /opt/app/llm/
COPY app/llm/plugins/ /opt/app/llm/plugins/
COPY app/settings/__init__.py app/settings/models.py /opt/app/settings/
COPY app/${AGENT_ROLE}/agent.py /opt/agent.py
RUN useradd --create-home --uid 10001 agent && mkdir -p /workspace/${WORKSPACE_NAME} && chown -R agent:agent /workspace
USER agent
# Mount points for Pi credentials must exist and belong to the agent.
RUN mkdir -p /home/agent/.pi/agent
WORKDIR /workspace/${WORKSPACE_NAME}
ENTRYPOINT ["python", "/opt/agent.py"]
