# Application image for the API and every worker; the service command selects the role.
# Its build context is filtered by Dockerfile.dockerignore. The allowlist in .dockerignore
# belongs to the agent images, which the coding workers build from this directory at run time.
FROM eclipse-temurin:21-jre AS java
FROM docker:29-cli AS docker
FROM node:24-slim AS node
FROM python:3.12-slim
# The codegen worker runs openapi-generator, which needs a JRE.
COPY --from=java /opt/java/openjdk /opt/java/openjdk
ENV JAVA_HOME=/opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:${PATH}"
# Docker client only. The coding and verification workers drive the daemon named by
# DOCKER_HOST; no daemon runs in this image.
COPY --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx
# The main worker clones session repositories and pushes their generated branches.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ADD --chmod=644 --checksum=sha256:3f1e6ce5c6ad4f15242c6170ab43aad4bad771622617eeece4a7d4f72ffaf329 \
    https://repo1.maven.org/maven2/org/openapitools/openapi-generator-cli/7.22.0/openapi-generator-cli-7.22.0.jar \
    tools/openapi-generator-cli-7.22.0.jar
# The API discovers Pi models and the workers run Pi for conversation workflows.
# Keep this version aligned with the isolated coding-agent image.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global @earendil-works/pi-coding-agent@1.6.0 \
        @langfuse/pi-observability-plugin@0.1.2 && npm cache clean --force
# The plugin that traces Pi sessions (app/llm/pi.py).
ENV PI_LANGFUSE_PLUGIN=/usr/local/lib/node_modules/@langfuse/pi-observability-plugin
COPY . .
# UID/GID 10001 is also the group of the sandbox daemon's socket.
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app:app /data
USER app
ENV PYTHONUNBUFFERED=1
ENV DOCUMENT_STORAGE_DIR=/data/documents
ENV GENERATED_CODE_DIR=/data/generated
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
