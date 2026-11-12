"""Provider error hierarchy. Each provider fails independently using these types."""


class ProviderError(Exception):
    """Base class for all provider failures."""


class ProviderNotConfiguredError(ProviderError):
    """Missing credentials or configuration (e.g. no API token)."""


class ProviderAuthError(ProviderError):
    """The provider rejected our credentials (HTTP 401)."""


class ProviderBlockedError(ProviderError):
    """CAPTCHA, bot challenge or access denied. Never retried, never bypassed."""


class ProviderRateLimitedError(ProviderError):
    """Still rate limited (HTTP 429) after polite retries."""


class ProviderTemporaryError(ProviderError):
    """Timeouts, network failures, 5xx responses after retries."""


class ProviderRequestError(ProviderError):
    """Invalid request or unexpected response; retrying would not help."""
