"""Only application-authored, credential-free messages belong in connector errors."""


class GitHubError(Exception):
    def __init__(self, status_code: int, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after
