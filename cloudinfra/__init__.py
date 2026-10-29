"""Cloud Infrastructure MCP: a provider-independent infrastructure service layer.

The Application Layer says what infrastructure it needs (compute, storage, network, database) in a provider-neutral
request; this package validates it, plans it, provisions it through a cloud provider adapter and reports it back. It
never reads a repository, builds an image, runs a test or decides how an application is released.
"""

__version__ = "1.37.1"
