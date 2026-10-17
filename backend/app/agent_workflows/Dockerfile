FROM maven:3.9.9-eclipse-temurin-21 AS java
FROM node:24-slim AS node
FROM python:3.12-slim
COPY --from=java /opt/java/openjdk /opt/java/openjdk
COPY --from=java /usr/share/maven /usr/share/maven
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:/usr/share/maven/bin:${PATH}"
# Node and the Pi CLI serve the Pi adapter; its credentials are mounted at run time.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global @earendil-works/pi-coding-agent@1.0.0 && npm cache clean --force
RUN pip install --no-cache-dir langchain==1.4.2 langchain-openai==1.6.6 langchain-google-genai==4.4.0 langfuse==4.16.0
# The Claude Agent SDK bundles the Claude Code CLI that serves the Claude adapter; its
# credentials arrive as environment variables at run time.
RUN pip install --no-cache-dir claude-agent-sdk==0.2.164
ARG AGENT_ROLE
ARG WORKSPACE_NAME
COPY app/__init__.py app/tracing.py /opt/app/
COPY app/agent_workflows/__init__.py app/agent_workflows/runtime.py /opt/app/agent_workflows/
COPY app/llm/__init__.py app/llm/factory.py app/llm/claude.py app/llm/claude_session.py app/llm/pi.py app/llm/pi_extension.ts app/llm/questions.py /opt/app/llm/
COPY app/settings/__init__.py app/settings/models.py /opt/app/settings/
COPY app/${AGENT_ROLE}/agent.py /opt/agent.py
RUN useradd --create-home --uid 10001 agent && mkdir -p /workspace/${WORKSPACE_NAME} && chown -R agent:agent /workspace
USER agent
# Mount points for Pi credentials must exist and belong to the agent.
RUN mkdir -p /home/agent/.pi/agent
WORKDIR /workspace/${WORKSPACE_NAME}
ENTRYPOINT ["python", "/opt/agent.py"]
