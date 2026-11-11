"""Integration failures with safe, credential-free descriptions."""


class CapabilityUnavailable(RuntimeError):
    """Missing configuration or local executable."""


class UnknownExternalResult(RuntimeError):
    """Request may have reached provider; never retry automatically."""


class ProviderError(RuntimeError):
    """Provider rejected request or returned invalid data."""


class InvalidStructuredAnswer(ProviderError):
    """The CLI completed; its retained final reply failed the business schema."""

    def __init__(self, recovery_path, answer_sha256, media):
        super().__init__("Agent已返回方案，但JSON格式或字段校验失败；原答案与费用回执已保留")
        self.recovery_path = recovery_path
        self.answer_sha256 = answer_sha256
        self.media = media


class ConfirmedJobFailure(ProviderError):
    """A durable provider receipt confirms a terminal job failure."""
