"""How to reach each kind of resource, written from what exists. Commands name secrets, never their values, and carry the
project and zone so they run as written. What a member needs at project level is listed as a prerequisite: the service
grants only on the resources it created."""
from ...models.resources import ConnectInfo, ResourceStatus

GCLOUD_PREREQUISITE = ("A project-level role with compute.projects.get and compute.instances.get, which the platform team grants once, "
                       "so that gcloud can find the machine")
IAP_PREREQUISITE = "A firewall rule admitting 35.235.240.0/20 on port 22; the request's network makes one when iap_ssh is on"
NOT_GRANTED = "Nobody has been granted access yet: grant it with manage_infrastructure_access"


def _secret_command(secret, project) -> str | None:
    return f"gcloud secrets versions access latest --secret={secret.name} --project {project}" if secret is not None else None


def _network_name(resource, by_id) -> str:
    network = by_id.get(getattr(resource, "network", None))
    return network.name if network is not None else "the operator's default network"


def info(resource, resources: list, project: str) -> ConnectInfo | None:
    kind = resource.kind.value
    by_id = {item.id: item for item in resources}
    stopped = ["It is stopped: start it with control_infrastructure first"] if resource.status == ResourceStatus.STOPPED else []
    if kind == "compute":
        notes = list(stopped)
        if resource.public_ip:
            notes.append(f"Also reachable at {resource.public_ip} on the ports its network opens")
        if resource.accelerator and not resource.profile:
            notes.append("It boots a plain image without GPU drivers; Secure Boot is off so that they can be installed")
        if resource.attached_disks:
            notes.append("Attached disks appear as /dev/disk/by-id/google-DISK_NAME; format and mount them before use")
        prerequisites = [GCLOUD_PREREQUISITE, IAP_PREREQUISITE] + ([] if resource.access else [NOT_GRANTED])
        return ConnectInfo(commands={"ssh": f"gcloud compute ssh {resource.name} --zone {resource.zone} --project {project} --tunnel-through-iap",
                                     "port_forward": f"gcloud compute start-iap-tunnel {resource.name} PORT --local-host-port=localhost:PORT "
                                                     f"--zone {resource.zone} --project {project}"},
                           prerequisites=prerequisites, notes=notes)
    if kind == "database":
        secret = by_id.get(resource.password_secret)
        host = resource.host or "HOST"
        if resource.engine == "mysql":
            client = f"mysql --host={host} --port={resource.port} --user={resource.username} --ssl-mode=REQUIRED --password {resource.database}"
        else:
            client = f'psql "host={host} port={resource.port} dbname={resource.database} user={resource.username} sslmode=require"'
        commands = {"connect": client}
        if secret is not None:
            commands["password"] = _secret_command(secret, project)
        where = ("Connect from a machine on the network " + _network_name(resource, by_id) + ", for example over ssh to a machine of this request"
                 if not resource.public_access else "Connections from outside go through the Cloud SQL Auth Proxy")
        return ConnectInfo(commands=commands, prerequisites=[where, "Read access to the password secret, which members of the request's access list have"],
                           notes=stopped + ["From a laptop: the Cloud SQL Auth Proxy, with a project-level cloudsql.client role that the platform team grants"])
    if kind == "cache":
        secret = by_id.get(resource.auth_secret)
        host = resource.host or "HOST"
        commands = {"connect": f'redis-cli -h {host} -p {resource.port} --tls --cacert server-ca.pem --askpass'}
        if secret is not None:
            commands["auth"] = _secret_command(secret, project)
        return ConnectInfo(commands=commands,
                           prerequisites=[f"A machine on the network {_network_name(resource, by_id)}: the cache has no public address"],
                           notes=["Save server_ca_cert from this resource as server-ca.pem; paste the AUTH string when redis-cli asks"])
    if kind == "secret":
        notes = {"database_password": ["The password of the request's database"], "cache_auth": ["The AUTH string of the request's cache"]}.get(resource.purpose, [])
        return ConnectInfo(commands={"read": _secret_command(resource, project)}, notes=notes + ([] if resource.access else [NOT_GRANTED]))
    if kind == "storage":
        if resource.type == "object":
            return ConnectInfo(commands={"list": f"gcloud storage ls {resource.url}", "copy": f"gcloud storage cp FILE {resource.url}/"},
                               notes=[] if resource.access else [NOT_GRANTED])
        machine = by_id.get(resource.attached_to)
        where = f"on {machine.name} as /dev/disk/by-id/google-{resource.name}" if machine is not None else "on no machine yet: attach it with attach_storage"
        return ConnectInfo(notes=[f"A block disk, attached {where}"])
    if kind == "queue":
        topic, subscription = resource.topic.rsplit("/", 1)[-1], (resource.subscription or "").rsplit("/", 1)[-1]
        commands = {"publish": f"gcloud pubsub topics publish {topic} --message=TEXT --project {project}"}
        if subscription:
            commands["pull"] = f"gcloud pubsub subscriptions pull {subscription} --auto-ack --project {project}"
        return ConnectInfo(commands=commands, notes=[] if resource.access else [NOT_GRANTED])
    if kind == "cluster":
        return ConnectInfo(commands={"credentials": f"gcloud container clusters get-credentials {resource.name} --location {resource.location} --project {project}"},
                           prerequisites=["A project-level container.clusterViewer role, which the platform team grants once", "The gke-gcloud-auth-plugin"])
    if kind == "load_balancer":
        address = resource.address or "ADDRESS"
        command = f"curl http://{address}:{resource.port}{resource.health_check_path or '/'}" if resource.type == "http" else f"nc -vz {address} {resource.port}"
        return ConnectInfo(commands={"try": command}, notes=["Plain http or tcp: TLS termination is not offered yet"])
    return None
