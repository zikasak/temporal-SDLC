// Loaded into every `pi` process started by ChatPi (app/llm/pi.py). It makes Pi act as a
// bare chat model: the caller's conversation and tools go in, one assistant message comes
// out, and the caller - not Pi - runs any tool the model asks for.
import { readFileSync } from "node:fs";

export default function (pi: any) {
	const input = JSON.parse(readFileSync(process.env.PI_LANGCHAIN_INPUT!, "utf8"));
	for (const tool of input.tools) {
		pi.registerTool({
			name: tool.name,
			label: tool.name,
			description: tool.description,
			parameters: tool.parameters,
			// Never does the work: ending the run here hands the call back to the caller.
			async execute() {
				return { content: [{ type: "text", text: "" }], details: undefined, terminate: true };
			},
		});
	}
	// The caller owns the conversation and its compaction. Pi's own would only summarize the
	// placeholder prompt, and its recovery from a response cut off at the output limit (or a
	// context overflow) is a second request, which the handler below aborts: the caller saw
	// "This operation was aborted" instead of the cut-off response. Cancelling it also
	// cancels that recovery request.
	pi.on("session_before_compact", async () => ({ cancel: true }));
	let requests = 0;
	let lastStopReason: string | undefined;
	pi.on("message_end", async (event: any) => {
		if (event.message?.role === "assistant") lastStopReason = event.message.stopReason;
	});
	// Replaces the placeholder prompt with the caller's history as native messages.
	pi.on("context", async (_event: unknown, ctx: any) => {
		// A call Pi rejects itself (such as invalid arguments) does not end the run, and
		// a second request would only repeat the first. A request following a failed one
		// is Pi's auto-retry of a transient error (rate limit, overload) and must go ahead:
		// aborting it would also hide the original error behind "operation was aborted".
		if (++requests > 1 && lastStopReason !== "error") ctx.abort();
		return { messages: input.messages };
	});
}
