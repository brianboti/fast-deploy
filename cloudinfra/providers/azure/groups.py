"""The request's resource group: one per request, the unit of ownership and the last thing a destroy removes.

Everything a request makes is in its group (learn.microsoft.com/azure/azure-resource-manager/management/overview: the
resources of a group "should share the same lifecycle"), named after the request and tagged with its ownership tags.
A network joined by a later request stays in its own group: a machine's interface in one group may use a subnet in
another. Each create makes sure the group exists, an idempotent PUT; a delete leaves it, and when nothing of the request
is left (CloudProvider.finished) the group goes too, with anything still in it that carries the request's tag, or no tag
and the request's name, such as a part of a machine whose own delete was still settling. Anything else in it, which this
service did not make, keeps the group and says so. A group this process has made is not written again for each of the
request's resources.
"""
import threading

from ...models.errors import ErrorCode, InfrastructureError
from ...observability.logging import event
from . import naming, translate
from .api import API_VERSIONS

VERSION = API_VERSIONS["Microsoft.Resources"]
GROUP_SECONDS = 1800  # a group's delete removes what is left in it, one resource at a time


def name_of(ctx) -> str:
    return naming.resource_group(ctx.name, ctx.request_id)


def group_of(resource_id: str) -> str:
    """The resource group in a resource id (/subscriptions/S/resourceGroups/G/providers/...)."""
    parts = (resource_id or "").split("/")
    lowered = [part.lower() for part in parts]
    return parts[lowered.index("resourcegroups") + 1] if "resourcegroups" in lowered else ""


def _ours(item: dict, ctx) -> bool:
    """A resource the request made: tagged with its id, or, untagged, named after it, as the parts Azure makes with a
    machine (its interface, address and OS disk) are, which carry no tags of their own."""
    tags = item.get("tags") or {}
    if tags.get("cloudinfra-request"):
        return tags["cloudinfra-request"] == ctx.request_id
    return str(item.get("name", "")).startswith(f"{ctx.name}-")


class AzureGroups:
    def __init__(self, api, ceiling):
        self.api, self.ceiling = api, ceiling
        self._made: set[str] = set()
        self._guard = threading.Lock()

    def id(self, group: str) -> str:
        return f"/subscriptions/{self.api.subscription_id}/resourceGroups/{group}"

    def url(self, group: str, path: str = "") -> str:
        return self.api.arm(self.id(group) + path)

    def ensure(self, ctx) -> str:
        """The request's group, made if it is not there yet; its name."""
        group = name_of(ctx)
        with self._guard:
            if group in self._made:
                return group
        self.api.put(self.url(group), {"location": ctx.region, "tags": translate.tags(ctx)}, VERSION, timeout=self.ceiling("resource_group"))
        with self._guard:
            self._made.add(group)
        return group

    def remove(self, ctx) -> None:
        """Delete the request's group once nothing it holds is foreign to the request."""
        group = name_of(ctx)
        with self._guard:
            self._made.discard(group)
        try:
            left = self.api.list(self.url(group, "/resources"), VERSION)
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return
            raise
        foreign = [item for item in left if not _ours(item, ctx)]
        if foreign:
            names = ", ".join(str(item.get("name")) for item in foreign[:5])
            raise InfrastructureError(ErrorCode.PROVIDER_CONFLICT, f"Resource group {group} holds resources this service did not make ({names}); it is kept. "
                                      "Move or delete them, then destroy the request again", {"container": f"resource group {group}", "group": group,
                                                                                              "resources": [item.get("id") for item in foreign]})
        if left:
            event("resource_group_leftovers", group=group, resources=[item.get("id") for item in left])
        self.api.delete(self.url(group), VERSION, timeout=self.ceiling("resource_group", GROUP_SECONDS))
        event("resource_group_deleted", group=group)
