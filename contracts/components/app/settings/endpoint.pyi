from dataclasses import dataclass
async def get_openai_endpoint() -> OpenAIEndpoint | None:
    ...
@dataclass(frozen=True)
class OpenAIEndpoint:
    base_url: str
    api_key: str
    display_name: str | None = None

    @property
    def label(self) -> str:
        ...
