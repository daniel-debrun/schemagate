class SchemagateError(Exception):
    """Base class for all schemagate errors."""


class SchemaDefinitionError(SchemagateError):
    """The target schema YAML is invalid."""


class IngestError(SchemagateError):
    """A source file could not be read."""


class ProviderResponseError(SchemagateError):
    """A model provider returned output that failed strict validation."""


class ApprovalPolicyError(SchemagateError):
    """An approval action violates the configured approval policy."""


class InvalidTransitionError(SchemagateError):
    """A governance state transition is not allowed from the current state."""


class UnapprovedSpecError(SchemagateError):
    """Materialization was attempted from a spec without a recorded, policy-satisfying approval."""


class DriftBlockedError(SchemagateError):
    """A source object is incompatible with the approved spec and cannot be materialized."""


class AuditChainError(SchemagateError):
    """The audit log hash chain failed verification."""


class NotFoundError(SchemagateError):
    """A referenced governance entity does not exist."""
