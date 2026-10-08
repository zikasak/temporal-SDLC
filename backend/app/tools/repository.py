"""Read-only access to the session's repository checkout for the interviewing models."""

from pathlib import Path

from langchain_core.tools import BaseTool

from app.agent_workflows.runtime import workspace_tools
from app.github.models import RepositorySelection

READ_ONLY = ('list_files', 'search_files', 'read_file')
GUIDANCE = (
    "\nThe project's Git repository is checked out for you. Before asking about or "
    "proposing anything its existing code may already settle, inspect it with "
    "list_files, search_files and read_file; paths are relative to the repository "
    "root, and these are the only files you can read. Rely on what the files show, "
    "never on guesses about their contents."
)


def chat_guidance(repository: RepositorySelection, available: bool) -> str:
    """What a conversation's model is told about the repository chosen for it."""
    name = f"{repository['full_name']} (branch {repository['branch']})"
    if not available:
        return (
            f"\nThe user chose the GitHub repository {name} for this conversation, but "
            "it could not be checked out, so you cannot read its files. When they ask "
            "about it, say so and suggest checking the GitHub connection; do not guess "
            "at its contents."
        )
    return (
        f"\nThis conversation is about the GitHub repository {name}, which is checked "
        "out for you: \"the project\", \"the code\" and \"the repository\" mean it unless "
        "the user says otherwise. Answer questions about it by inspecting it with "
        "list_files, search_files and read_file, starting from its README and "
        "top-level layout when you need an overview; never ask the user to upload or "
        "paste what the repository already holds. Paths are relative to the "
        "repository root, and these are the only repository files you can read; you "
        "cannot change them. Rely on what the files show, never on guesses about "
        "their contents."
    )


def build(root: Path) -> list[BaseTool]:
    return [item for item in workspace_tools(root) if item.name in READ_ONLY]
