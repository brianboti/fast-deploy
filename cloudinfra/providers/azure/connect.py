"""How to reach each kind of resource on Azure, written from what exists. Commands carry the subscription and the resource
group so they run as written; what a member needs beyond the grants on the resource itself is a prerequisite: the
service grants only on what it created."""
from ...models.resources import ConnectInfo, ResourceStatus
from .groups import group_of
from .network import reaches_internet

NOT_GRANTED = "Nobody has been granted access yet: grant it with manage_infrastructure_access"
SIGN_IN = "Sign in with your own Entra account: the machine has no password, and no key anyone holds"
NO_SIGN_IN = ("Entra sign-in is not installed: the machine reaches no package host (its network has no NAT, or the operator denies egress, and it has no "
              "public address), and the sign-in extension needs one. Reach it with Run Command (az vm run-command invoke), which needs a role the platform "
              "team grants, or give the network a NAT")
GRANT_DELAY = "A new grant can take up to 10 minutes to apply"


def _bastion(settings) -> tuple[str, str, str] | None:
    """The operator's Bastion as (subscription, resource group, name)."""
    if not settings.bastion:
        return None
    parts = settings.bastion.split("/")
    return parts[2], group_of(settings.bastion), parts[-1]


def info(resource, resources: list, settings, subscription: str, drivers: bool = False) -> ConnectInfo | None:
    kind = resource.kind.value
    by_id = {item.id: item for item in resources}
    stopped = ["It is stopped (deallocated): start it with control_infrastructure first"] if resource.status == ResourceStatus.STOPPED else []
    granted = [] if resource.access else [NOT_GRANTED]
    if kind == "compute":
        if not reaches_internet(by_id.get(resource.network), bool(resource.public_ip)):
            return ConnectInfo(notes=[NO_SIGN_IN, *stopped], prerequisites=granted)
        notes = [SIGN_IN, *stopped]
        if resource.public_ip:
            notes.append(f"Also reachable at {resource.public_ip} on the ports its network opens")
        if resource.accelerator and not drivers:
            notes.append("It boots an image without GPU drivers; Secure Boot is off so that they can be installed")
        if resource.attached_disks:
            notes.append("Attached disks appear under /dev/disk/azure/scsi1/ as lun0, lun1…; format and mount them before use")
        bastion = _bastion(settings)
        if bastion is not None:
            where = f"--name {bastion[2]} --resource-group {bastion[1]} --subscription {bastion[0]} --target-resource-id {resource.provider_id}"
            commands = {"ssh": f"az network bastion ssh {where} --auth-type AAD",
                        "port_forward": f"az network bastion tunnel {where} --resource-port PORT --port PORT"}
            prerequisites = ["Reader on the Bastion, which the platform team grants once", "The Azure CLI's ssh and bastion extensions "
                             "(az extension add --name ssh; az extension add --name bastion)", GRANT_DELAY]
        else:
            address = resource.public_ip or resource.internal_ip or "ADDRESS"
            commands = {"ssh": f"az ssh vm --ip {address} --subscription {subscription}"}
            where = "its public address" if resource.public_ip else "its private address, so it needs a path into the machine's network"
            prerequisites = [f"The operator has no Bastion: az ssh vm reaches {where}", "The Azure CLI's ssh extension (az extension add --name ssh)", GRANT_DELAY]
        return ConnectInfo(commands=commands, prerequisites=prerequisites + granted, notes=notes)
    if kind == "storage":
        if resource.type == "object":
            account = f"--account-name {resource.name} --container-name data --auth-mode login"
            return ConnectInfo(commands={"list": f"az storage blob list {account} --output table",
                                         "copy": f"az storage blob upload {account} --name NAME --file FILE"},
                               notes=[f"Data roles can take up to 10 minutes to apply after a grant; {resource.url} takes no account key, only Entra sign-in",
                                      *granted])
        machine = by_id.get(resource.attached_to)
        where = f"on {machine.name}, under /dev/disk/azure/scsi1/" if machine is not None else "on no machine yet: attach it with attach_storage"
        return ConnectInfo(notes=[f"A managed disk, attached {where}"])
    if kind == "identity":
        notes = [f"Holds the request's grants at {resource.level}; it has no secret, and none is ever made", "Machines of a group that names it carry it"]
        commands = {}
        if resource.client_id:
            commands["token"] = f"curl -H Metadata:true 'http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=RESOURCE&client_id={resource.client_id}'"
            notes.append(f"On such a machine, a workload asks for its tokens with client id {resource.client_id} (AZURE_CLIENT_ID for the Azure libraries)")
        return ConnectInfo(commands=commands, notes=notes)
    return None
