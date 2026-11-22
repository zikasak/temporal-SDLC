"""Turns a stored conversation into the model prompt."""

import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

INLINE_DOCUMENT_CHARS = 8000
MAX_REMEMBERED_CHARS = 12000

# Told in a session's system prompt, which then stays the same on every turn.
CURRENT_STATE_GUIDANCE = (
    "\nThe latest user message ends with the session's current state in <current_state>; "
    "it is always up to date."
)


def with_current_state(messages: list[BaseMessage], state: str) -> list[BaseMessage]:
    """Add what changes from turn to turn (recorded drafts, open problems) to the latest
    user message. Each turn is a new provider request, which reuses the cached prompt only
    up to its first change: state in the system prompt would have every turn write the
    whole prompt to the cache again."""
    block = f"<current_state>\n{state}\n</current_state>"
    if messages and isinstance(messages[-1], HumanMessage):
        return [*messages[:-1], HumanMessage(content=f"{messages[-1].text}\n\n{block}")]
    return [*messages, HumanMessage(content=block)]


def changes_since(baseline: Mapping[str, Any], current: Mapping[str, Any],
                  keys: Mapping[str, Callable[[Any], str]]) -> dict[str, Any]:
    """What `current` changed of `baseline`. A list named in `keys` gives the items added
    or replaced, in full, and the keys of those removed; any other field its new value.
    A state that starts from a large existing one (a service's whole API) is sent as
    these changes, beside the unchanging original in the cached system prompt."""
    changes: dict[str, Any] = {}
    for name in sorted(baseline.keys() | current.keys()):
        before, after = baseline.get(name), current.get(name)
        if name not in keys:
            if before != after:
                changes[name] = after
            continue
        key = keys[name]
        old = {key(item): item for item in before or []}
        new = {key(item): item for item in after or []}
        recorded = [item for item_key, item in new.items() if old.get(item_key) != item]
        removed = [item_key for item_key in old if item_key not in new]
        if recorded or removed:
            changes[name] = {"recorded": recorded, "removed": removed}
    return changes


def model_messages(
    messages: list[dict[str, object]],
    generated_documents: list[dict[str, object]] | None = None,
    inline_texts: dict[str, str] | None = None,
    remembered_passages: list[dict[str, object]] | None = None,
    extra_guidance: str = "",
) -> tuple[list[BaseMessage], set[str]]:
    inline_texts = inline_texts or {}
    prompt: list[BaseMessage] = []
    document_ids: set[str] = set()
    for item in messages:
        content = str(item["content"])
        attachment = item.get("attachment")
        if item["role"] == "user":
            if isinstance(attachment, dict):
                document_id = attachment.get("document_id")
                if isinstance(document_id, str) and re.fullmatch(r"[0-9a-f]{32}", document_id):
                    document_ids.add(document_id)
                    content += (
                        f"\n\nAttached document URI: document://{document_id}\n"
                        f"Filename: {attachment.get('filename', 'document')}"
                    )
                    if document_id in inline_texts:
                        content += (
                            "\nFull document text (complete; no need to read it again):\n"
                            f"<document>\n{inline_texts[document_id]}\n</document>"
                        )
            prompt.append(HumanMessage(content=content))
        else:
            prompt.append(AIMessage(content=content))
    generated_documents = generated_documents or []
    guidance = (
        "Use create_document to make a text document, edit_document to replace one "
        "exact text match, replace_document to replace all content, and "
        "delete_document to remove a model-created document. Uploaded "
        "documents are read-only. Before answering about a document's contents, "
        "use read_document or search_document; do not invent contents. "
        "Never ask the user whether or how to read or search a document; just do it. "
        "search_document matches individual keywords, so search for short distinctive "
        "terms (names, numbers) rather than whole sentences. If a search finds nothing, "
        "retry with other keywords or synonyms, then read the document with "
        "read_document starting at offset 0 and following next_offset until you find "
        "the answer or reach total_characters. Reply only once you have the answer or "
        "have checked the whole document; if it is not there, say so. "
        "Model-created documents can be edited or deleted only within this conversation. "
        "After a document tool call, briefly report its result to the user."
    ) + extra_guidance + CURRENT_STATE_GUIDANCE
    inventory = [
        {"uri": f"document://{item['document_id']}",
         "filename": item["filename"], "revision": item["revision"]}
        for item in generated_documents
    ]
    if remembered_passages and prompt and isinstance(prompt[-1], HumanMessage):
        context = (
            "\n\n<retrieved_context>\nPassages you retrieved earlier in this "
            "conversation (use them before searching again; they may not cover "
            "the whole document):"
        )
        for passage in remembered_passages:
            context += (
                f"\n<passage uri=\"document://{passage['document_id']}\" "
                f"offset=\"{passage['offset']}\">\n{passage['text']}\n</passage>"
            )
        prompt[-1] = HumanMessage(content=str(prompt[-1].content) + context
                                  + "\n</retrieved_context>")
    prompt.insert(0, SystemMessage(content=guidance))
    prompt = with_current_state(prompt, "Current model-created documents: "
                                + json.dumps(inventory, ensure_ascii=False))
    return prompt, document_ids
