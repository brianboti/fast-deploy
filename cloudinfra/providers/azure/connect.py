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
        if resource.kubernetes_service_account and resource.client_id:
            namespace, name = resource.kubernetes_service_account.split("/", 1)
            commands["kubernetes"] = (f"kubectl create serviceaccount {name} -n {namespace} && kubectl annotate serviceaccount {name} -n {namespace} "
                                      f"azure.workload.identity/client-id={resource.client_id}")
            notes.append(f"In the request's clusters, pods running as the service account {resource.kubernetes_service_account} and labelled "
                         "azure.workload.identity/use: \"true\" act as it")
        return ConnectInfo(commands=commands, notes=notes)
    if kind == "secret":
        notes = {"database_password": ["The password of the request's database"], "cache_auth": ["The access key of the request's cache"]}.get(resource.purpose, [])
        if not resource.versions and not resource.purpose:
            notes.append("It has no value yet: a member at admin sets one with the write command; nothing can be read until then")
        return ConnectInfo(commands={"read": _secret_command(resource),
                                     "write": f"az keyvault secret set --vault-name {_vault(resource)} --name {resource.name} --file FILE"},
                           notes=notes + [GRANT_DELAY, *granted])
    if kind == "database":
        secret = by_id.get(resource.password_secret)
        host = resource.host or "HOST"
        if resource.engine == "mysql":
            client = f"mysql --host={host} --port={resource.port} --user={resource.username} --ssl-mode=REQUIRED --password {resource.database}"
        else:
            client = f'psql "host={host} port={resource.port} dbname={resource.database} user={resource.username} sslmode=require"'
        commands = {"connect": client}
        if secret is not None:
            commands["password"] = _secret_command(secret)
        if resource.public_access:
            where = (f"The server takes no connection until a firewall rule admits one: az {'mysql' if resource.engine == 'mysql' else 'postgres'} flexible-server "
                     f"firewall-rule create --resource-group {group_of(resource.provider_id)} --name {resource.name} --rule-name RULE "
                     "--start-ip-address ADDRESS --end-ip-address ADDRESS")
        else:
            network = by_id.get(resource.network)
            where = (f"A machine on the network {network.name if network is not None else resource.network}: the server has no public address, and its name "
                     "resolves to its private endpoint there")
        days = 30 if resource.engine == "mysql" else 7
        return ConnectInfo(commands=commands, prerequisites=[where, "Read access to the password secret, which members of the request's access list have"],
                           notes=stopped + [f"The user is the server's administrator; Azure starts a stopped server again by itself after {days} days"]
                           + (["A small database is Burstable: not for production"] if resource.tier.startswith("Standard_B") else []))
    if kind == "cache":
        secret = by_id.get(resource.auth_secret)
        commands = {"connect": f"redis-cli -h {resource.host or 'HOST'} -p {resource.port} --tls --askpass"}
        if secret is not None:
            commands["auth"] = _secret_command(secret)
        network = by_id.get(resource.network)
        return ConnectInfo(commands=commands, prerequisites=[f"A machine on the network {network.name if network is not None else resource.network}: the cache has no "
                                                             "public address, and its name resolves to its private endpoint there"],
                           notes=["Paste the access key when redis-cli asks; the certificate is a public one, so no CA file is needed"])
    if kind == "queue":
        namespace, topic = resource.topic.split("/namespaces/", 1)[1].split("/topics/", 1)
        public = settings.cloud == "public"  # Service Bus's host and audience are documented for the public cloud only
        base = f"https://{namespace}.servicebus.windows.net/{topic}" if public else f"https://NAMESPACE_HOST/{topic}"
        audience = "https://servicebus.azure.net" if public else "SERVICE_BUS_AUDIENCE"
        sign_in = f"$(az account get-access-token --resource {audience} --query accessToken --output tsv)"  # a shell substitution, not a credential
        commands = {"publish": f'curl -X POST {base}/messages -H "Authorization: Bearer {sign_in}" -d TEXT'}
        if resource.subscription:
            commands["receive"] = f'curl -X DELETE {base}/subscriptions/{resource.subscription.rsplit("/", 1)[-1]}/messages/head -H "Authorization: Bearer {sign_in}"'
        return ConnectInfo(commands=commands, notes=["Entra sign-in only: the namespace has no connection string", GRANT_DELAY, *granted])
    if kind == "registry":
        registry = resource.address.split(".", 1)[0]
        return ConnectInfo(commands={"login": f"az acr login --name {registry}",
                                     "digest": f"az acr repository show --name {registry} --image {resource.repository}/IMAGE:TAG --query digest --output tsv",
                                     "pull": f"docker pull {resource.address}/IMAGE@sha256:DIGEST"},
                           notes=[f"Images live under {resource.address}/ in the operator's registry; only the operator's registry writers, named at admin, push, "
                                  "and every other member pulls", "A release names an image here by digest, never by tag",
                                  "Deleting the registry deletes every image under it", *granted])
    if kind == "cluster":
        where = f"--resource-group {group_of(resource.provider_id)} --name {resource.name} --subscription {subscription}"
        namespace = resource.namespace or "NAMESPACE"
        return ConnectInfo(commands={"credentials": f"az aks get-credentials {where}", "sign_in": "kubelogin convert-kubeconfig -l azurecli",
                                     "use": f"kubectl get pods -n {namespace}"},
                           prerequisites=["kubelogin (az aks install-cli)", "An address the operator's authorized ranges admit, when it lists any",
                                          GRANT_DELAY, *granted],
                           notes=[f"Your access is in the namespace {namespace} only: RBAC Reader at use, Writer at admin",
                                  "Sign-in is Entra's: the cluster has no local accounts, and its kubeconfig holds no secret",
                                  "A Service of type LoadBalancer or NodePort, an Ingress and a Gateway are refused in the namespace: a public address is the "
                                  "infrastructure layer's"])
    if kind == "service":
        network = by_id.get(resource.network)
        where = f"a machine on the network {network.name if network is not None else resource.network}"
        url = resource.url or "URL"
        if resource.public:
            commands, notes = {"call": f"curl {url}"}, ["It takes calls without sign-in from its network"]
        else:
            client = settings.service_auth_client_id or "CLIENT_ID"
            sign_in = f"$(az account get-access-token --resource api://{client} --query accessToken --output tsv)"  # a shell substitution, not a credential
            commands = {"call": f'curl -H "Authorization: Bearer {sign_in}" {url}'}
            notes = ["A call needs an Entra token for the operator's app registration and a member granted use or admin: anyone else is answered 401 or 403",
                     *granted]
        return ConnectInfo(commands=commands, prerequisites=[f"Reach it from {where}: its environment is internal"],
                           notes=notes + [f"It runs a stand-in until a release is put on it; revision {resource.revision or 'none yet'}"])
    if kind == "job":
        where = f"--resource-group {group_of(resource.provider_id)} --name {resource.name} --subscription {subscription}"
        notes = ["It runs a stand-in until a release is put on it", "Starting a run is admin's: Azure's start can change the image and command it runs "
                 "as the job's identity; use reads the job and its runs", *granted]
        if resource.schedule:
            notes.insert(0, f"It runs by itself on {resource.schedule['cron']} (UTC)")
        return ConnectInfo(commands={"run": f"az containerapp job start {where}", "runs": f"az containerapp job execution list {where} --output table"},
                           prerequisites=["The Azure CLI's containerapp extension (az extension add --name containerapp)", GRANT_DELAY], notes=notes)
    if kind == "load_balancer":
        domains = [item for item in resource.components if item.get("kind") == "custom_domain"]
        if not domains:
            return ConnectInfo(commands={"call": f"curl http://{resource.address or 'ADDRESS'}:{resource.port}" if resource.type == "http"
                                         else f"nc -vz {resource.address or 'ADDRESS'} {resource.port}"},
                               notes=[f"Public on port {resource.port}, passed through to the machines, which see each client's own address"])
        door_id = next((item.get("front_door_id") for item in resource.components if item.get("kind") == "front_door"), None)
        records = []
        for domain in domains:
            records.append(f"{domain.get('txt_name', '_dnsauth.' + domain['name'])} TXT {domain.get('txt') or 'TOKEN (not issued yet)'}")
            records.append(f"{domain['name']} CNAME {domain.get('cname') or resource.address}")
        return ConnectInfo(commands={"call": f"curl https://{domains[0]['name']}"},
                           prerequisites=["DNS records the operator adds: " + "; ".join(records)],
                           notes=[f"Certificate: {resource.certificate or 'PROVISIONING'}; Front Door approves each hostname once its TXT record resolves, "
                                  "and serves HTTPS after its CNAME does", "HTTP is redirected to HTTPS; Front Door forwards to the machines over HTTP",
                                  "The machines admit Front Door's backends only, but every profile's, any tenant's: the application refuses a request "
                                  f"whose X-Azure-FDID header is not {door_id or 'this profile id'}"])
    return None


def _vault(resource) -> str:
    return resource.resource_name.split("//", 1)[1].split(".", 1)[0]


def _secret_command(secret) -> str:
    return f"az keyvault secret show --vault-name {_vault(secret)} --name {secret.name} --query value --output tsv"
