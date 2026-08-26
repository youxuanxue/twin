class WorkspaceBusyError(RuntimeError):
    """Raised when a workspace is already being mutated by another process."""
