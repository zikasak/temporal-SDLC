# Trusted runner of the component tests: the suite, written from the component contract's
# stubs alone, run in-process against the service's sources, which only this build adds.
# The tests run without a network; only this build installs or downloads anything.
FROM python:3.12-slim
RUN useradd --create-home --uid 10001 tester && mkdir /tests && chown tester:tester /tests
# The suite's own dependencies first: the service's, installed after them, win any
# conflict, so the code under test runs with the versions it declares.
COPY suite/requirements.txt /runner/suite-requirements.txt
RUN pip install --no-cache-dir -r /runner/suite-requirements.txt
COPY repository /repository
ARG REQUIREMENTS=""
RUN for file in $REQUIREMENTS; do \
        (cd "/repository/$(dirname "$file")" && pip install --no-cache-dir -r "$(basename "$file")") || exit 1; \
    done
# The runner's own plugin, installed last so nothing the suite or the service lists
# replaces it: it stops a test that outlasts its limit, so one hang cannot use up the
# time of the whole run.
RUN pip install --no-cache-dir pytest-timeout==2.3.1
COPY run-tests.sh prepare.py /runner/
ARG SOURCE_ROOT=.
ENV PYTHONPATH=/repository/${SOURCE_ROOT} PYTHONDONTWRITEBYTECODE=1
USER tester
# Fetched now, as the tests cannot: the test servers of an in-process Temporal
# environment, when the code uses Temporal. They are kept in /tmp, where the environment
# looks for them, so a run must not replace /tmp.
RUN python /runner/prepare.py
COPY --chown=tester:tester suite /tests
WORKDIR /tests
RUN python -m pytest --collect-only -q
ENTRYPOINT ["bash", "/runner/run-tests.sh"]
CMD ["python", "-m", "pytest", "--timeout=30", "--timeout-method=signal", "--junitxml=/tests/reports/junit.xml"]
