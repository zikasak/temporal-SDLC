# Async execution

The Python backend uses one asyncio event loop per API or worker process. Container
agent entry points and the local development launcher also enter through
`asyncio.run(main())`. The TypeScript frontend keeps its native promises.

Call `await app.agent_workflows.runtime.run_agent(profile)` from Python. Model
requests, tool calls, and history compaction use `ainvoke`. The Pi adapter provides
native async subprocess execution; its required LangChain sync interface is a
compatibility bridge for callers outside an event loop.

Subprocesses use `asyncio.create_subprocess_exec` or `create_subprocess_shell`.
Timeouts and cancellation stop and reap the process before resources are released.
Agent shell commands run in their own process group so their descendants stop too.
The development launcher waits for child exits and signals without polling.

Filesystem, archive, parser, and language-specific source operations expose only
synchronous APIs. Async callers offload them through `app.asyncio_utils.run_sync`.
This helper propagates context variables and waits for an in-flight operation to
finish before propagating cancellation, preventing file closure or temporary tree
cleanup from racing a writer. Keep these operations bounded: Python cannot forcibly
stop a worker thread. Use `async_context` to acquire and release synchronous file
and temporary-directory contexts off the event loop. Workspace tools retain their
sync LangChain interface and provide the same cancellation-safe async boundary.

Pure validation and in-memory transformations remain ordinary functions. Password
derivation stays in its existing bounded executor to keep CPU and memory use
limited. The pinned upstream Claude observability hook remains a standalone
synchronous plugin; its source is tracked separately in `UPSTREAM.md`.

Run backend checks from `backend/`, so pytest loads the asyncio configuration:

```sh
python -m pytest -q
```
