# Component tests

This standalone pytest project tests behaviour the HTTP contract tests cannot observe, at
seams of the service's code: the interfaces described by the stubs in
`contracts/components/` of the repository. The tests import the stubbed modules and drive
them in-process, with fakes of every collaborator the stubs name. They never fake the seam
itself.

Run them from this directory with the service's sources importable and its dependencies
installed:

```sh
pip install -r requirements.txt
PYTHONPATH=<the service's source root> python -m pytest
```

The trusted Docker runner builds the suite with the service's sources and dependencies,
collects it during the build and runs it without a network when started. When the code
uses Temporal, the build fetches the servers of `temporalio.testing.WorkflowEnvironment`
(`start_time_skipping` and `start_local`) so the tests can start them offline. Each test is
limited to 30 seconds and pytest to ten minutes; the JUnit XML report, written to
`/tests/reports/junit.xml`, holds the tests that ran even when the ten minutes run out.
