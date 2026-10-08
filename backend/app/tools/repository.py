"""Read-only access to the session's repository checkout for the interviewing models."""

from pathlib import Path

from langchain_core.tools import BaseTool

from app.agent_workflows.runtime import workspace_tools

READ_ONLY = ('list_files', 'search_files', 'read_file')
GUIDANCE = (
    "\nThe project's Git repository is checked out for you. Before asking about or "
    "proposing anything its existing code may already settle, inspect it with "
    "list_files, search_files and read_file; paths are relative to the repository "
    "root, and these are the only files you can read. Rely on what the files show, "
    "never on guesses about their contents."
)


def build(root: Path) -> list[BaseTool]:
    return [item for item in workspace_tools(root) if item.name in READ_ONLY]
