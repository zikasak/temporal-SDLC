"""Trusted part of the component test runner's build: an in-process Temporal environment
downloads its test server the first time it starts, which the tests, run without a
network, cannot. When the service's code uses Temporal, start each kind of environment
once, so its server is kept in the image for the tests.
"""
import asyncio
import importlib.util
import sys


async def main() -> int:
    if importlib.util.find_spec('temporalio') is None:
        return 0
    from temporalio.testing import WorkflowEnvironment
    for start in (WorkflowEnvironment.start_time_skipping, WorkflowEnvironment.start_local):
        environment = await start()
        await environment.shutdown()
    return 0


if __name__ == '__main__':
    sys.exit(asyncio.run(main()))
