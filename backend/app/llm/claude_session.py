"""A native Claude conversation bridged to the authoring agent's synchronous loop."""
import asyncio
import tempfile
import uuid
from concurrent.futures import Future
from contextlib import suppress
from threading import Event, Thread

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, ResultMessage,
    StreamEvent, ToolUseBlock, create_sdk_mcp_server,
)
from langchain_core.messages import SystemMessage, ToolMessage


class ClaudeSession:
    """Keep Claude's own history, including thinking and injected context, in one CLI.

    The caller still executes tools. A PostToolUse hook holds their results until the
    next invocation supplies them, so the CLI never continues with placeholder output.
    The CLI and its anyio task group live on one worker task for their whole lifetime.
    """

    def __init__(self, model):
        self.model = model
        self.directory = tempfile.TemporaryDirectory(prefix='claude-agent-')
        self.thread = None
        self.expected = None

    def close(self):
        self._stop()
        self.directory.cleanup()

    def _stop(self):
        if self.thread is not None:
            if not self.loop.is_closed():
                with suppress(RuntimeError):
                    self.loop.call_soon_threadsafe(self.stop.set)
            self.thread.join()
            self.thread = None
        self.expected = None

    def invoke(self, messages, tools):
        # Compaction replaces history deliberately. Start a new native conversation
        # from that checkpoint; ordinary tool turns must only append to the history.
        if self.expected is not None and messages[:len(self.expected)] != self.expected:
            self._stop()
        if self.thread is None:
            self.ready = Event()
            self.reply = Future()
            self.thread = Thread(target=self._worker, args=(messages, tools), daemon=True)
            self.thread.start()
            self.ready.wait()
        else:
            self.reply = Future()
        try:
            if self.failure is not None:
                raise self.failure
            if self.expected is not None:
                asyncio.run_coroutine_threadsafe(self._continue(messages), self.loop).result()
            result = self.reply.result(timeout=self.model.timeout)
        except TimeoutError as exc:
            self._stop()
            raise TimeoutError(f'claude did not finish within {self.model.timeout:g} seconds') from exc
        except BaseException:
            self._stop()
            raise
        self.expected = [*messages, result.generations[0].message]
        return result

    async def _continue(self, messages):
        if self.failure is not None:
            raise self.failure
        added = messages[len(self.expected):]
        if self.waiting_tools:
            results = {message.tool_call_id: message for message in added
                       if isinstance(message, ToolMessage)}
            if set(results) != set(self.waiting_tools):
                raise ValueError('Claude needs one result for every pending tool call')
            for identifier, future in self.waiting_tools.items():
                future.set_result(results[identifier])
        else:
            if not added or any(not isinstance(message.content, str) for message in added):
                raise ValueError('Claude needs a text follow-up')
            await self.client.query('\n\n'.join(message.text for message in added))

    def _worker(self, messages, tools):
        try:
            asyncio.run(self._run(messages, tools))
        except BaseException as exc:
            self.failure = exc
            if not self.reply.done():
                self.reply.set_exception(exc)
        finally:
            self.ready.set()

    async def _run(self, messages, tools):
        # Imports here keep the ordinary chat adapter independent of this lifecycle.
        from app.llm.claude import (
            TOOL_PREFIX, TOOL_SERVER, _claude_messages, _handed_back, _History,
            _transcript, CONTINUE_PROMPT, _chat_result,
        )
        self.loop = asyncio.get_running_loop()
        self.stop = asyncio.Event()
        self.failure = None
        self.waiting_tools = {}
        history = _claude_messages(messages)
        prompt = CONTINUE_PROMPT
        if isinstance(history[-1]['content'], str):
            prompt = history.pop()['content']
        session = str(uuid.uuid4())
        system = '\n\n'.join(message.text for message in messages
                             if isinstance(message, SystemMessage))

        async def tool_result(data, tool_use_id, context):
            message = await self.waiting_tools[data['tool_use_id']]
            event = data['hook_event_name']
            field = 'updatedToolOutput' if event == 'PostToolUse' else 'additionalContext'
            return {'hookSpecificOutput': {'hookEventName': event, field: message.text}}

        options = ClaudeAgentOptions(
            model=self.model.model, cli_path=self.model.cli_path,
            cwd=self.directory.name, system_prompt=system, tools=[], setting_sources=[],
            skills=[], strict_mcp_config=True, verbatim_prompts=True,
            extra_args={'no-session-persistence': None}, include_partial_messages=True,
            env={'DISABLE_AUTO_COMPACT': '1'},
            mcp_servers={TOOL_SERVER: create_sdk_mcp_server(
                TOOL_SERVER, tools=[_handed_back(item) for item in tools])},
            allowed_tools=[TOOL_PREFIX + item['name'] for item in tools],
            # A failed MCP validation must also wait for the application loop. The
            # loop can compact history before supplying results, so allow that delay.
            hooks={event: [HookMatcher(matcher=TOOL_PREFIX + '.*',
                                       hooks=[tool_result], timeout=86400)]
                   for event in ('PostToolUse', 'PostToolUseFailure')},
            session_store=_History(_transcript(history, session, self.directory.name))
            if history else None,
            resume=session if history else None,
        )
        async with ClaudeSDKClient(options=options) as client:
            self.client = client
            self.ready.set()
            await client.query(prompt)
            # Cancellation exits this context in the task that entered it.
            async def receive():
                blocks, usage, model_name = [], {}, self.model.model
                async for event in client.receive_messages():
                    if isinstance(event, AssistantMessage) and event.parent_tool_use_id is None:
                        blocks.extend(event.content)
                        model_name = event.model
                    elif isinstance(event, StreamEvent) and event.parent_tool_use_id is None:
                        raw = event.event
                        if raw['type'] == 'message_start':
                            usage = dict(raw['message'].get('usage') or {})
                        elif raw['type'] == 'message_delta':
                            usage.update(raw.get('usage') or {})
                        elif raw['type'] == 'message_stop':
                            calls = [block for block in blocks if isinstance(block, ToolUseBlock)]
                            if calls:
                                self.waiting_tools = {block.id: self.loop.create_future()
                                                      for block in calls}
                                self.reply.set_result(_chat_result(
                                    blocks, model_name, usage, 'tool_use'))
                                blocks = []
                    elif isinstance(event, ResultMessage):
                        if self.stop.is_set():
                            continue
                        if event.is_error:
                            raise RuntimeError('claude response failed: ' + (
                                '; '.join(event.errors or []) or event.result or event.subtype))
                        self.waiting_tools = {}
                        self.reply.set_result(_chat_result(
                            blocks, model_name, usage or event.usage or {},
                            event.stop_reason))
                        blocks = []
                raise RuntimeError('claude session closed before its next response')

            receiver = asyncio.create_task(receive())
            stopper = asyncio.create_task(self.stop.wait())
            try:
                done, _ = await asyncio.wait([receiver, stopper],
                                             return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    await client.interrupt()
                else:
                    await receiver
            finally:
                receiver.cancel()
                stopper.cancel()
                await asyncio.gather(receiver, stopper, return_exceptions=True)
