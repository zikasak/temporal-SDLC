from dataclasses import dataclass
@dataclass(frozen=True)
class ConversationRef:
    name: str
    restore_from: str | None = None
    context: str = ''

    @property
    def source(self) -> str:
        ...
