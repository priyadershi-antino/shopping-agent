"""Application error hierarchy.

`transient` marks failures worth retrying (timeouts, rate limits, 5xx).
"""


class ShoppingAgentError(Exception):
    transient: bool = False


class ConfigurationError(ShoppingAgentError):
    pass


class ProviderError(ShoppingAgentError):
    def __init__(self, provider: str, message: str, *, transient: bool = False) -> None:
        super().__init__(f"{provider}: {message}")
        self.provider = provider
        self.transient = transient


class RateLimitedError(ProviderError):
    def __init__(self, provider: str, message: str = "rate limited") -> None:
        super().__init__(provider, message, transient=True)


class QuotaExhaustedError(ProviderError):
    """The provider's configured request budget for the current window is spent."""

    def __init__(self, provider: str) -> None:
        super().__init__(provider, "quota budget exhausted", transient=False)


class ValidationFailedError(ShoppingAgentError):
    """Model or provider output failed schema validation."""


class UnsafeUrlError(ShoppingAgentError):
    """A URL was rejected by the URL guard."""


class LLMUnavailableError(ShoppingAgentError):
    """Every configured LLM route failed."""


class LLMRefusalError(ShoppingAgentError):
    """The model declined the request."""


class ToolInputError(ShoppingAgentError):
    """Tool arguments are valid in shape but refer to something that doesn't exist."""
