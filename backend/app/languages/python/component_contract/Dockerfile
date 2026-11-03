# Trusted runner of the component contract's conformance check: the repository's code with
# its dependencies, and the stubs, compared by mypy's stubtest. The check runs without a
# network; only this build installs anything.
FROM python:3.12-slim
ARG MYPY
RUN pip install --no-cache-dir "$MYPY"
RUN useradd --create-home --uid 10001 checker
COPY repository /repository
ARG REQUIREMENTS=""
RUN for file in $REQUIREMENTS; do \
        (cd "/repository/$(dirname "$file")" && pip install --no-cache-dir -r "$(basename "$file")") || exit 1; \
    done
COPY components /components
COPY check.sh literals.py /
ARG SOURCE_ROOT=.
ENV PYTHONPATH=/repository/${SOURCE_ROOT} MYPYPATH=/components PYTHONDONTWRITEBYTECODE=1
USER checker
# Not beside any source: mypy prefers modules of its working directory to the stubs.
WORKDIR /home/checker
ENTRYPOINT ["bash", "/check.sh"]
