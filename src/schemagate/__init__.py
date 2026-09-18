"""schemagate: governed schema mapping for sender-controlled spreadsheets."""

from schemagate.config import ConfidencePolicy
from schemagate.errors import (
    ApprovalPolicyError,
    DriftBlockedError,
    ProviderResponseError,
    SchemagateError,
    UnapprovedSpecError,
)
from schemagate.models import MappingProposal, Tier

__version__ = "0.1.0"

__all__ = [
    "ApprovalPolicyError",
    "ConfidencePolicy",
    "DriftBlockedError",
    "MappingProposal",
    "ProviderResponseError",
    "SchemagateError",
    "Tier",
    "UnapprovedSpecError",
    "__version__",
]
