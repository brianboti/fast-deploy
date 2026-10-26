"""Shared wire limits; importing this module never executes a resolver."""
MAX_INPUT = 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024


class ResolutionPending(Exception):
    def __init__(self, identifier):
        self.identifier = identifier
        super().__init__("Remote dependency resolution is pending")

    def view(self):
        return {"state": "resolving", "operation_id": self.identifier,
                "next_action": "Check this operation with status, then repeat the proposal to use its validated result. Changed source creates a new operation."}
