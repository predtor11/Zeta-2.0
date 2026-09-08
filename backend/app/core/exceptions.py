"""Zeta exception hierarchy.  Tools raise these; the orchestrator converts them into honest user-facing messages."""


class ZetaError(Exception):
    """Base class for expected failures."""

    user_message: str = "Something went wrong."

    def __init__(self, message: str = "", *, user_message: str = ""):
        super().__init__(message or user_message or self.user_message)
        if user_message:
            self.user_message = user_message
        elif message:
            self.user_message = message


class ConfigurationError(ZetaError):
    user_message = "Zeta is not configured correctly."


class ProviderError(ZetaError):
    user_message = "The AI provider could not be reached."


class ProviderUnavailable(ProviderError):
    pass


class ToolError(ZetaError):
    user_message = "The tool failed."


class ToolNotFound(ToolError):
    pass


class ToolValidationError(ToolError):
    user_message = "Invalid arguments for the tool."


class ToolTimeout(ToolError):
    user_message = "The operation timed out."


class PermissionDenied(ZetaError):
    user_message = "That action is not permitted by the current permission policy."


class PathNotAllowed(PermissionDenied):
    user_message = "That path is outside the folders Zeta is allowed to access."


class ConfirmationDenied(ZetaError):
    user_message = "The action was not confirmed."


class ConfirmationTimeout(ZetaError):
    user_message = "The confirmation timed out."


class TaskCancelled(ZetaError):
    user_message = "The task was cancelled."


class NotImplementedCapability(ZetaError):
    """Raised by tools/providers whose capability is scaffolded but NOT IMPLEMENTED."""

    user_message = "This capability is NOT IMPLEMENTED yet."
