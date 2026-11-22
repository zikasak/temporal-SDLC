# The API as one self-contained service, which the pipeline builds from the repository root
# and verifies with contract-tests/: its own PostgreSQL, no Temporal, LLM or GitHub, served
# on 8080 by a non-root user. Deployments use backend/Dockerfile instead.

# Build stage: the dependencies, installed apart from the runtime image. No tests run here.
FROM python:3.12-slim AS build
WORKDIR /build
COPY backend/requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM node:24-slim AS node

# Runtime stage.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends postgresql \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s "$(ls -d /usr/lib/postgresql/*/bin)" /opt/postgresql
COPY --from=build /install /usr/local
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && npm install --global @earendil-works/pi-coding-agent@1.0.0 && npm cache clean --force
WORKDIR /app
COPY backend/app app
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app:app /data
USER app
RUN /opt/postgresql/initdb --pgdata=/data/postgres --username=app --auth=trust --encoding=UTF8
ENV PYTHONUNBUFFERED=1
ENV APP_DATABASE_URL=postgresql://app@127.0.0.1:5432/postgres
ENV DOCUMENT_STORAGE_DIR=/data/documents
ENV GENERATED_CODE_DIR=/data/generated
EXPOSE 8080
# Sessions are signed with a per-start secret unless AUTH_JWT_SECRET is supplied (losing it
# only ends sign-in sessions). The saved OpenAI API key is encrypted with
# SETTINGS_ENCRYPTION_KEY; when it is not supplied, a key is generated once and kept with
# the data it encrypts (/data/settings-encryption-key, readable by the app user only), so
# the saved key stays readable across restarts.
CMD ["sh", "-ec", "/opt/postgresql/pg_ctl --pgdata=/data/postgres --wait --silent --log=/data/postgres.log -o '-c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp' start\nexport AUTH_JWT_SECRET=\"${AUTH_JWT_SECRET:-$(python -c 'import secrets; print(secrets.token_urlsafe(48))')}\"\nif [ -z \"${SETTINGS_ENCRYPTION_KEY:-}\" ]; then\n  keyfile=/data/settings-encryption-key\n  if [ ! -s \"$keyfile\" ]; then\n    (umask 077 && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())' > \"$keyfile.tmp\" && mv \"$keyfile.tmp\" \"$keyfile\")\n  fi\n  SETTINGS_ENCRYPTION_KEY=\"$(cat \"$keyfile\")\"\n  export SETTINGS_ENCRYPTION_KEY\nfi\nexec uvicorn app.api.main:app --host 0.0.0.0 --port 8080"]
