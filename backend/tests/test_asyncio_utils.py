"""Cancellation must finish filesystem work before closing its resources."""

import asyncio
import threading

import pytest

from app.asyncio_utils import async_context, run_sync


@pytest.mark.parametrize('fail', [False, True])
async def test_cancelled_io_finishes_before_its_file_is_closed(tmp_path, fail):
    started = threading.Event()
    release = threading.Event()
    path = tmp_path / 'result.txt'

    def write(stream):
        started.set()
        if not release.wait(5):
            raise TimeoutError('Test did not release the writer')
        stream.write('complete')
        if fail:
            raise OSError('Disk failure after writing')

    async def save():
        async with async_context(path.open, 'w') as stream:
            await run_sync(write, stream)

    task = asyncio.create_task(save())
    try:
        async with asyncio.timeout(5):
            while not started.is_set():
                await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.sleep(0)
        # Cancellation must not close the file under the still-running writer.
        assert not task.done()
        task.cancel()  # Repeated cancellation must preserve the same guarantee.
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert path.read_text() == 'complete'
