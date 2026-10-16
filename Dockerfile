# The API as one self-contained service, which the pipeline builds from the repository root
# and verifies with contract-tests/: its own PostgreSQL, no Temporal, LLM or GitHub, served
# on 8080 by a non-root user. Deployments use backend/Dockerfile instead.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends postgresql \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s "$(ls -d /usr/lib/postgresql/*/bin)" /opt/postgresql
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app app
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app:app /data
USER app
RUN /opt/postgresql/initdb --pgdata=/data/postgres --username=app --auth=trust --encoding=UTF8
ENV PYTHONUNBUFFERED=1
ENV APP_DATABASE_URL=postgresql://app@127.0.0.1:5432/postgres
ENV DOCUMENT_STORAGE_DIR=/data/documents
ENV GENERATED_CODE_DIR=/data/generated
EXPOSE 8080
# Sessions are signed with a key of this container's own unless one is supplied.
CMD ["sh", "-ec", "/opt/postgresql/pg_ctl --pgdata=/data/postgres --wait --silent --log=/data/postgres.log -o '-c listen_addresses=127.0.0.1 -c unix_socket_directories=/tmp' start\nexport AUTH_JWT_SECRET=\"${AUTH_JWT_SECRET:-$(python -c 'import secrets; print(secrets.token_urlsafe(48))')}\"\nexec uvicorn app.api.main:app --host 0.0.0.0 --port 8080"]
