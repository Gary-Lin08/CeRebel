"""Python SDK for the Cerebel motion-analysis API.

    from cerebel import CerebelClient
    client = CerebelClient("https://cerebel.tech/playground")
    client.login("you@example.com", "…")
    result = client.analyze("swing.mp4")
    print(result["main_fix"]["headline"])
"""

from cerebel._version import __version__
from cerebel.analysis import fix_summary, main_fix
from cerebel.client import DEFAULT_BASE_URL, CerebelClient
from cerebel.errors import (
    AnalysisFailed,
    AuthError,
    CerebelError,
    ConsentRequired,
    Forbidden,
    Gone,
    NotFound,
    PaymentRequired,
    RateLimited,
    ServiceUnavailable,
    UploadRejected,
)

__all__ = [
    "DEFAULT_BASE_URL",
    "AnalysisFailed",
    "AuthError",
    "CerebelClient",
    "CerebelError",
    "ConsentRequired",
    "Forbidden",
    "Gone",
    "NotFound",
    "PaymentRequired",
    "RateLimited",
    "ServiceUnavailable",
    "UploadRejected",
    "__version__",
    "fix_summary",
    "main_fix",
]
