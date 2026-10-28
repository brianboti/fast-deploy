"""What every tool module shares: registration against the catalog, and the parameter annotation helper."""
from typing import Annotated, Any

import pydantic

from ..catalog import BY_NAME


def field(tool: str, name: str):
    """The Annotated metadata of one parameter, described by the catalog record."""
    return pydantic.Field(description=BY_NAME[tool].describe(name))


def register(mcp, wrap, fn):
    """Register a function as the catalog tool of the same name: title, description and safety hint come from the record."""
    spec = BY_NAME[fn.__name__]
    return mcp.tool(name=spec.name, title=spec.title, description=spec.model_description, annotations=spec.annotations, structured_output=True)(wrap(fn))


Result = dict[str, Any]
Wait = Annotated[int, pydantic.Field(ge=0, le=45, description="Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running.")]
