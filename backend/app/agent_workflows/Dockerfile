FROM maven:3.9.9-eclipse-temurin-21 AS java
FROM python:3.12-slim
COPY --from=java /opt/java/openjdk /opt/java/openjdk
COPY --from=java /usr/share/maven /usr/share/maven
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:/usr/share/maven/bin:${PATH}"
RUN pip install --no-cache-dir langchain==1.4.2 langchain-openai==1.6.6 langchain-google-genai==4.4.0 langfuse==4.16.0
ARG AGENT_ROLE
ARG WORKSPACE_NAME
COPY app/__init__.py app/tracing.py /opt/app/
COPY app/agent_workflows/__init__.py app/agent_workflows/runtime.py /opt/app/agent_workflows/
COPY app/llm/__init__.py app/llm/factory.py /opt/app/llm/
COPY app/${AGENT_ROLE}/agent.py /opt/agent.py
RUN useradd --create-home --uid 10001 agent && mkdir -p /workspace/${WORKSPACE_NAME} && chown -R agent:agent /workspace
USER agent
WORKDIR /workspace/${WORKSPACE_NAME}
ENTRYPOINT ["python", "/opt/agent.py"]
