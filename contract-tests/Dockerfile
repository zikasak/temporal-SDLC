FROM python:3.12-slim
RUN useradd --create-home --uid 10001 tester && mkdir /tests && chown tester:tester /tests
WORKDIR /tests
COPY --chown=tester:tester requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=tester:tester . .
USER tester
RUN python -m pytest --collect-only -q
ENTRYPOINT ["bash", "/tests/run-tests.sh"]
CMD ["python", "-m", "pytest", "--junitxml=/tests/reports/junit.xml"]
