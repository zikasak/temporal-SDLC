// Loaded into every `pi` process started by ChatPi (app/llm/pi.py). Pi runs the flow
// natively: the flow's tools come from the session's MCP server, and after each model
// turn the session decides whether Pi goes on (a paused flow or a spent turn budget
// stops it) and what the model reads before its next request. A summary of the history
// keeps what the stage's checkpoint prompt asks for, and is followed by what the session
// pins: the task's inputs and notes, which the summary would otherwise replace.
import { readFileSync } from "node:fs";
import { compact } from "@earendil-works/pi-coding-agent";

export default function (pi: any) {
	const config = JSON.parse(process.env.PI_FLOW_CONFIG!);
	// The session's token; the MCP server's header reads it by reference.
	const token = process.env.PI_FLOW_TOKEN!;
	// The flow's own prompt, exactly: none of Pi's, which describes its coding tools and
	// names its working directory.
	const systemPrompt = readFileSync(config.systemPrompt, "utf8");
	pi.on("before_agent_start", async () => ({ systemPrompt }));
	// The OpenAI-compatible endpoint saved in Settings, as a provider of this process
	// only. Its key stays in the environment: Pi reads it from the `$NAME` reference.
	if (config.provider) {
		const { name, model, ...provider } = config.provider;
		pi.registerProvider(name, {
			...provider,
			apiKey: "$PI_FLOW_API_KEY",
			models: [{ ...model, input: ["text"], cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
		});
	}
	if (config.tools) {
		pi.registerMcpServer(config.server, {
			url: `${config.url}/mcp`,
			headers: { Authorization: "Bearer ${PI_FLOW_TOKEN}" },
			// Declared to the model like built-in tools, and callable from codemode scripts too.
			exposure: "direct",
			timeout: config.toolTimeout,
		});
	}
	if (config.compactionPrompt) {
		const instructions = readFileSync(config.compactionPrompt, "utf8");
		// Pi's own summary, as its automatic compaction makes it, with the stage's
		// instructions; any failure leaves it to Pi's own summary.
		pi.on("session_before_compact", async (event: any, ctx: any) => {
			const model = ctx.model;
			if (!model) return;
			try {
				const auth = await ctx.modelRegistry.getApiKeyAndHeaders(model);
				if (!auth.ok) return;
				const compaction = await compact(
					event.preparation,
					auth.baseUrl ? { ...model, baseUrl: auth.baseUrl } : model,
					auth.apiKey,
					auth.headers,
					instructions,
					event.signal,
					pi.getThinkingLevel(),
				);
				return { compaction };
			} catch (error) {
				if (event.signal?.aborted) throw error;
				console.error(`The stage's summary failed; Pi summarizes on its own: ${error}`);
			}
		});
	}
	if (config.pinned) {
		// Asked for once per summary, so the requests that follow it stay the same.
		let pinned: { summary: number; text: string } | undefined;
		pi.on("context", async (event: any) => {
			const messages = event.messages;
			const at = messages.findLastIndex((message: any) => message.role === "compactionSummary");
			if (at < 0) return;
			const summary = messages[at].timestamp;
			if (pinned?.summary !== summary) {
				try {
					const response = await fetch(`${config.url}/pinned`, {
						method: "POST",
						headers: { Authorization: `Bearer ${token}` },
					});
					if (!response.ok) throw new Error(`HTTP ${response.status}`);
					pinned = { summary, text: (await response.json()).text };
				} catch (error) {
					console.error(`The flow did not say what follows the summary: ${error}`);
					return;
				}
			}
			const message = {
				role: "custom",
				customType: "flow-pinned",
				content: pinned!.text,
				display: false,
				timestamp: summary,
			};
			return { messages: [...messages.slice(0, at + 1), message, ...messages.slice(at + 1)] };
		});
	}
	pi.on("turn_end", async (event: any, ctx: any) => {
		let decision: any;
		try {
			const response = await fetch(`${config.url}/turn`, {
				method: "POST",
				headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
				body: JSON.stringify({
					stopReason: event.message?.stopReason,
					toolResults: event.toolResults?.length ?? 0,
				}),
			});
			if (!response.ok) throw new Error(`HTTP ${response.status}`);
			decision = await response.json();
		} catch (error) {
			// Unchecked, a paused flow or a spent turn budget would go on: stop instead.
			ctx.abort();
			throw new Error(`The flow did not answer after the turn: ${error}`);
		}
		if (decision.stop) {
			// Before the next request: the run ends with the tool results it has.
			ctx.abort();
			return;
		}
		if (decision.notices?.length) {
			return {
				entries: [
					...event.entries,
					...decision.notices.map((content: string) => ({
						type: "custom_message",
						customType: "flow-notice",
						content,
						display: false,
					})),
				],
			};
		}
	});
}
