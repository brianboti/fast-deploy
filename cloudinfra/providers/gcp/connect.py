"""How to reach each kind of resource, written from what exists. Commands name secrets, never their values, and carry the
project and zone so they run as written. What a member needs at project level is listed as a prerequisite: the service
grants only on the resources it created."""
from ...models.resources import ConnectInfo, ResourceStatus

GCLOUD_PREREQUISITE = ("A project-level role with compute.projects.get and compute.instances.get, which the platform team grants once, "
                       "so that gcloud can find the machine")
IAP_PREREQUISITE = ("A firewall rule admitting 35.235.240.0/20 on port 22, and on every port forwarded with port_forward: the request's network makes the "
                    "one for port 22 when iap_ssh is on, and configure_firewall adds others")
NOT_GRANTED = "Nobody has been granted access yet: grant it with manage_infrastructure_access"


def _secret_command(secret, project) -> str | None:
    return f"gcloud secrets versions access latest --secret={secret.name} --project {project}" if secret is not None else None


def _network_name(resource, by_id) -> str:
    network = by_id.get(getattr(resource, "network", None))
    return network.name if network is not None else "the operator's default network"


def info(resource, resources: list, project: str, drivers: bool = False) -> ConnectInfo | None:
    """resources: the request's own, and those of other requests that it names (a network joined, a machine a disk is
    attached to). drivers: whether a machine's image carries its GPU drivers."""
    kind = resource.kind.value
    by_id = {item.id: item for item in resources}
    stopped = ["It is stopped: start it with control_infrastructure first"] if resource.status == ResourceStatus.STOPPED else []
    if kind == "compute":
        notes = list(stopped)
        if resource.public_ip:
            notes.append(f"Also reachable at {resource.public_ip} on the ports its network opens")
        if resource.accelerator and not drivers:
            notes.append("It boots an image without GPU drivers; Secure Boot is off so that they can be installed")
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
        machine = any(item.kind.value == "compute" and item.request_id == resource.request_id and item.status != ResourceStatus.DELETED for item in resources)
        where = ("Connect from a machine on the network " + _network_name(resource, by_id) + (", for example over ssh to a machine of this request" if machine else "")
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
        notes = [f"A block disk, attached {where}"]
        if resource.snapshot_schedule:
            notes.append(f"Snapshotted daily, each kept {resource.snapshot_keep_days} days; deleting the disk deletes them")
            return ConnectInfo(commands={"snapshots": f"gcloud compute snapshots list --filter=labels.cloudinfra-disk={resource.name} --project {project}"}, notes=notes)
        return ConnectInfo(notes=notes)
    if kind == "queue":
        topic, subscription = resource.topic.rsplit("/", 1)[-1], (resource.subscription or "").rsplit("/", 1)[-1]
        commands = {"publish": f"gcloud pubsub topics publish {topic} --message=TEXT --project {project}"}
        if subscription:
            commands["pull"] = f"gcloud pubsub subscriptions pull {subscription} --auto-ack --project {project}"
        return ConnectInfo(commands=commands, notes=[] if resource.access else [NOT_GRANTED])
    if kind == "cluster":
        commands = {"credentials": f"gcloud container clusters get-credentials {resource.name} --location {resource.location} --project {project}"}
        notes = list(stopped)
        if resource.namespace:
            commands["namespace"] = f"kubectl config set-context --current --namespace {resource.namespace}"
            notes.append(f"Access is granted in namespace {resource.namespace} only: use binds the ClusterRole view there, admin binds edit")
        return ConnectInfo(commands=commands, notes=notes + ([] if resource.access else [NOT_GRANTED]),
                           prerequisites=["A project-level container.clusterViewer role, which the platform team grants once", "The gke-gcloud-auth-plugin"])
    if kind == "service":
        url = resource.url or "URL"
        if resource.public:
            return ConnectInfo(commands={"call": f"curl {url}"}, notes=["Callable without signing in: the operator allowed it and the request asked for it"])
        return ConnectInfo(commands={"call": f'curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" {url}'},
                           notes=[f"Ingress {resource.ingress}; a caller needs the invoker role, which use grants"] + ([] if resource.access else [NOT_GRANTED]))
    if kind == "warehouse":
        return ConnectInfo(commands={"tables": f"bq ls {project}:{resource.dataset}", "query": f"bq query --use_legacy_sql=false --project_id {project} 'SELECT 1'"},
                           prerequisites=["Running queries needs bigquery.jobs.create on a project (roles/bigquery.jobUser), which the platform team grants once"],
                           notes=[] if resource.access else [NOT_GRANTED])
    if kind == "nosql":
        return ConnectInfo(commands={"describe": f"gcloud firestore databases describe --database={resource.database} --project {project}"},
                           prerequisites=["Firestore has no policy per database: the platform team grants roles/datastore.user on the project once, with an IAM "
                                          f'condition resource.name == "projects/{project}/databases/{resource.database}"'],
                           notes=[f"Clients name the database {resource.database}, not (default)"])
    if kind == "budget":
        budget = (resource.budget or "").rsplit("/", 1)[-1]
        account = (resource.billing_account or "").rsplit("/", 1)[-1]
        return ConnectInfo(commands={"describe": f"gcloud billing budgets describe {budget} --billing-account={account}"},
                           notes=[f"Counts the costs labelled cloudinfra-request={resource.watches}, which every resource of that request carries; billing data, "
                                  "and so an alert, arrives some hours late", "It alerts and stops nothing"])
    if kind == "monitor":
        return ConnectInfo(commands={"policies": f"gcloud monitoring policies list --project {project} --filter='user_labels.cloudinfra-request={resource.request_id}'"},
                           notes=[f"Checks {item['url']} every {item['period_seconds']} seconds" for item in resource.checks]
                           + [f"Alerts {', '.join(resource.notify)} when a check fails"])
    if kind == "dns":
        servers = ", ".join(resource.name_servers) or "its name servers"
        return ConnectInfo(commands={"check": f"dig +short NS {resource.dns_name}", "records": f"gcloud dns record-sets list --zone {resource.name} --project {project}"},
                           notes=[f"Point {resource.dns_name} at {servers} at the domain's registrar, once"] + ([] if resource.access else [NOT_GRANTED]))
    if kind == "identity":
        notes = [f"Holds the request's grants at {resource.level}; it has no key, and none is ever made"]
        commands = {}
        if resource.kubernetes_service_account:
            namespace, name = resource.kubernetes_service_account.split("/", 1)
            commands["kubernetes"] = (f"kubectl create serviceaccount {name} -n {namespace} && kubectl annotate serviceaccount {name} -n {namespace} "
                                      f"iam.gke.io/gcp-service-account={resource.email}")
            notes.append(f"Pods using the Kubernetes service account {resource.kubernetes_service_account} act as it")
        return ConnectInfo(commands=commands, notes=notes + ["Machines of a group that names it run as it"])
    if kind == "job":
        region = resource.region
        notes = [f"Runs by itself on {resource.schedule['cron']} ({resource.schedule['time_zone']})"] if resource.schedule else ["Runs only when asked"]
        return ConnectInfo(commands={"run": f"gcloud run jobs execute {resource.name} --region {region} --project {project}",
                                     "runs": f"gcloud run jobs executions list --job {resource.name} --region {region} --project {project}"},
                           notes=notes + ([] if resource.access else [NOT_GRANTED]))
    if kind == "load_balancer":
        address = resource.address or "ADDRESS"
        command = f"curl http://{address}:{resource.port}{resource.health_check_path or '/'}" if resource.type == "http" else f"nc -vz {address} {resource.port}"
        if resource.hostnames:
            return ConnectInfo(commands={"try": command, "https": f"curl https://{resource.hostnames[0]}{resource.health_check_path or '/'}"},
                               notes=[f"HTTPS for {', '.join(resource.hostnames)} once their DNS points at {address}; the certificate is {resource.certificate or 'not reported'}"])
        return ConnectInfo(commands={"try": command}, notes=["Plain http or tcp; give the load balancer hostnames for HTTPS"])
    return None
