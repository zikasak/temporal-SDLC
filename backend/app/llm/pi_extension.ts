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
	let requests = 0;
	// Replaces the placeholder prompt with the caller's history as native messages.
	pi.on("context", async (_event: unknown, ctx: any) => {
		// A call Pi rejects itself (such as invalid arguments) does not end the run, and
		// a second request would only repeat the first.
		if (++requests > 1) ctx.abort();
		return { messages: input.messages };
	});
}
