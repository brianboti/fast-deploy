"""The MCP service catalog: one record per tool, the single source of the registered tools and of docs/MCP-CATALOG.md.

A test holds the registered tool set, their parameters and their safety hints to these records, and the rendered document
to this file, so the catalog a reviewer reads is the catalog the server serves.
"""
import re
from dataclasses import dataclass

from mcp.types import ToolAnnotations

from ..models.errors import ErrorCode

MEANINGS = {
    ErrorCode.INVALID_REQUEST: "The request does not fit the contract: a missing or unknown field, a wrong type, nothing asked for.",
    ErrorCode.UNSUPPORTED_PROVIDER: "No adapter for that provider name, or the operator has not configured it.",
    ErrorCode.INVALID_REGION: "Not a region, not allowed by the operator, or a zone outside it.",
    ErrorCode.INVALID_RESOURCE_NAME: "A name outside the lowercase-letters-digits-dashes rule.",
    ErrorCode.INVALID_COMPUTE_CONFIGURATION: "A machine shape the provider has no type for.",
    ErrorCode.INVALID_STORAGE_CONFIGURATION: "A storage request the provider cannot satisfy (attaching a bucket, a disk across zones).",
    ErrorCode.INVALID_NETWORK_CONFIGURATION: "A range, port or source outside the rules, or a public address without a public network.",
    ErrorCode.INVALID_DATABASE_CONFIGURATION: "An engine version the provider does not run, or a private database without a network.",
    ErrorCode.INVALID_CACHE_CONFIGURATION: "A cache version the provider does not run, or a cache without a network.",
    ErrorCode.INVALID_CLUSTER_CONFIGURATION: "A node shape the provider has no type for.",
    ErrorCode.INVALID_LOAD_BALANCER_CONFIGURATION: "No machines behind it, or machines in different zones or regions.",
    ErrorCode.UNSUPPORTED_RESOURCE_KIND: "A request block of a kind the contract or the chosen adapter does not provide; details name the nearest kind.",
    ErrorCode.LIMIT_EXCEEDED: "Above one of the operator's limits on counts, sizes or live requests.",
    ErrorCode.COST_LIMIT_EXCEEDED: "The estimate from list prices is above the ceiling for this service or this caller.",
    ErrorCode.QUOTA_EXCEEDED: "The provider's quota in the region has no room for the request.",
    ErrorCode.NOT_FOUND: "No such request, resource or operation is visible to this caller.",
    ErrorCode.PERMISSION_DENIED: "The caller may not do this (can_provision, can_destroy), or did not authenticate.",
    ErrorCode.INVALID_STATE: "The resource or request is not in a state that allows this (still provisioning, already destroyed, a network in use).",
    ErrorCode.PROVIDER_AUTH_FAILED: "The service could not get or use its own provider credential.",
    ErrorCode.PROVIDER_PERMISSION_DENIED: "The service's identity lacks a permission at the provider.",
    ErrorCode.PROVIDER_CONFLICT: "The provider reports a conflict: a name in use, a bucket with objects, a precondition failed.",
    ErrorCode.PROVIDER_UNAVAILABLE: "The provider could not be reached, is rate limiting, or answered 5xx; retry later.",
    ErrorCode.PROVIDER_ERROR: "The provider refused the request for another reason; its message is in details.",
    ErrorCode.OPERATION_TIMEOUT: "A provider operation did not finish within operation_timeout_seconds; check the status.",
    ErrorCode.PROVISIONING_FAILED: "A request failed while provisioning; details name the cause and what cleanup did.",
    ErrorCode.DESTROY_FAILED: "Some resources could not be deleted; details name them, and the call can be repeated.",
    ErrorCode.INTERNAL_ERROR: "The service stopped on an unexpected exception; nothing was retried.",
}

HINTS = {
    "read_only": ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False),
    "changes_cloud": ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False),
    "destructive": ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=False),
}
HINT_WORDS = {"read_only": "Read-only; creates and changes nothing.", "changes_cloud": "Creates cloud resources that cost money; needs can_provision.",
              "destructive": "Deletes cloud resources; needs can_destroy."}


@dataclass(frozen=True)
class Input:
    name: str
    description: str
    required: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    group: str
    title: str
    purpose: str
    inputs: tuple[Input, ...]
    output: str
    errors: tuple[str, ...]
    security: str
    mapping: str
    hint: str = ""

    def __post_init__(self):
        if not self.hint:
            object.__setattr__(self, "hint", hint_of(self.name))

    def describe(self, name: str) -> str:
        return next(item.description for item in self.inputs if item.name == name)

    @property
    def model_description(self) -> str:
        """What the model reads: the purpose, what comes back, the safety rule, and what to call next. Claude's tool guidance
        asks for at least three or four sentences; the catalog's columns are those sentences."""
        security = re.sub(r"^Needs can_(provision|destroy)\.\s*", "", self.security)  # the hint sentence already says it
        parts = [self.purpose, f"Returns {self.output}", f"{HINT_WORDS[self.hint]} {security}".rstrip()]
        waits = any(item.name == "wait_seconds" for item in self.inputs)
        if waits and self.hint == "destructive":
            parts.append("It answers at once with the deletion running, or waits up to wait_seconds; follow it with get_infrastructure_status "
                         "or get_operation until the resource is DELETED or the request DESTROYED.")
        elif waits:
            parts.append("It answers at once with the operation running, or waits up to wait_seconds; follow it with get_infrastructure_status "
                         "or get_operation until the status is READY or FAILED.")
        return " ".join(part.rstrip() if part.endswith(".") else part.rstrip() + "." for part in parts)

    @property
    def annotations(self) -> ToolAnnotations:
        return HINTS[self.hint]


def hint_of(name: str) -> str:
    """The safety hint follows the verb: create, provision, resize, attach and configure change the cloud; delete and destroy are destructive; the rest read."""
    verb = name.split("_", 1)[0]
    if verb in ("delete", "destroy"):
        return "destructive"
    if verb in ("create", "provision", "resize", "attach", "configure"):
        return "changes_cloud"
    return "read_only"


# ---- shared inputs
REGION = Input("region", "The provider's region (us-central1); must be in the service's allowed regions.", True)
PROVIDER = Input("provider", "The cloud provider adapter; gcp (default) is the one implemented.")
ZONE = Input("zone", "A zone of the region; the provider picks the first when omitted.")
NAME = Input("name", "A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted.")
LABELS = Input("labels", "Labels for every resource, next to the service's ownership labels.")
WAIT = Input("wait_seconds", "Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running.")
RESOURCE_ID = Input("resource_id", "The resource id this service issued.", True)
REQUEST_ID = Input("request_id", "The request id this service issued (32 hex characters).", True)
NETWORK_ID = Input("network_id", "A network this service created earlier (net-…), to join instead of the operator's default network.")
PRIVATE_NETWORK_ID = Input("network_id", "A network this service created earlier (net-…); required, since the resource is private.", True)
COMMON_ERRORS = ("PERMISSION_DENIED", "UNSUPPORTED_PROVIDER", "INVALID_REGION", "INVALID_RESOURCE_NAME", "LIMIT_EXCEEDED", "COST_LIMIT_EXCEEDED", "QUOTA_EXCEEDED",
                 "PROVIDER_AUTH_FAILED", "PROVIDER_PERMISSION_DENIED", "PROVIDER_CONFLICT", "PROVIDER_UNAVAILABLE", "PROVIDER_ERROR", "OPERATION_TIMEOUT", "PROVISIONING_FAILED")
READ_ERRORS = ("NOT_FOUND", "PERMISSION_DENIED", "PROVIDER_AUTH_FAILED", "PROVIDER_PERMISSION_DENIED", "PROVIDER_UNAVAILABLE", "PROVIDER_ERROR")
DELETE_ERRORS = ("NOT_FOUND", "PERMISSION_DENIED", "INVALID_STATE", "PROVIDER_AUTH_FAILED", "PROVIDER_PERMISSION_DENIED", "PROVIDER_CONFLICT", "PROVIDER_UNAVAILABLE",
                 "PROVIDER_ERROR", "OPERATION_TIMEOUT", "DESTROY_FAILED")
RESPONSE = "InfrastructureResponse: request_id, status, the resources with ids, names, addresses and states, endpoints, operations, the cost estimate, and error when FAILED."
RESOURCE = "ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one."
OWNERSHIP = "Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own."

CATALOG: tuple[ToolSpec, ...] = (
    # ---- compute
    ToolSpec("create_compute", "Compute", "Create virtual machines",
             "Create one or more identical virtual machines: CPUs, memory, operating system family, boot disk, optional public address. No image, script, key or "
             "command is taken; access is through the provider's IAM-based login.",
             (REGION, Input("cpu", "Virtual CPUs per machine.", True), Input("memory_gb", "Memory per machine, in GB.", True), PROVIDER,
              Input("count", "How many identical machines (default 1)."), Input("os", "ubuntu (default), debian or rocky."), Input("boot_disk_gb", "Boot disk in GB (default 20)."),
              Input("public_ip", "Give each machine a public address (default false)."), NETWORK_ID,
              Input("accelerator", "GPUs per machine: t4, l4, a100 or h100; the machine family follows the type, and the driver is the Application Layer's to install."),
              Input("accelerator_count", "GPUs per machine (default 1); each type offers fixed counts."), ZONE, NAME, LABELS, WAIT),
             RESPONSE, ("INVALID_COMPUTE_CONFIGURATION", "INVALID_NETWORK_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", *COMMON_ERRORS),
             f"Needs can_provision. Machines get OS Login (IAM) and no service account; vTPM and integrity monitoring on, and Secure Boot on unless a GPU is attached "
             f"(the provider's driver installation needs it off). {OWNERSHIP}",
             "Compute Engine: compute.instances.insert with an E2 machine type (N1, G2, A2 or A3 with a GPU) and a public image family; compute.instances.get after."),
    ToolSpec("get_compute", "Compute", "Get a machine", "One machine's live state: status, addresses, machine type, attached disks.",
             (RESOURCE_ID,), RESOURCE, READ_ERRORS, "Read-only; the caller sees its own requests' resources (scope: all sees every one).", "Compute Engine: compute.instances.get."),
    ToolSpec("list_compute", "Compute", "List machines", "Every machine this service created, as the provider sees them now, optionally for one request.",
             (Input("request_id", "Only the machines of this request."),), "instances: a list of machines with their addresses and states; count; the labels used to select them.",
             READ_ERRORS, "Read-only; selects by the ownership labels, so a caller with scope own sees its own machines only.",
             "Compute Engine: compute.instances.aggregatedList with a label filter."),
    ToolSpec("resize_compute", "Compute", "Resize a machine",
             "Change a machine's CPUs and memory: it is stopped, resized and started; its addresses stay unless the provider reassigns an ephemeral one.",
             (RESOURCE_ID, Input("cpu", "The new number of virtual CPUs.", True), Input("memory_gb", "The new memory, in GB.", True), WAIT),
             RESOURCE, ("INVALID_COMPUTE_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", "LIMIT_EXCEEDED", "PERMISSION_DENIED", "PROVIDER_ERROR", "PROVIDER_UNAVAILABLE", "OPERATION_TIMEOUT"),
             "Needs can_provision. The machine is unavailable while it restarts.", "Compute Engine: compute.instances.stop, setMachineType, start."),
    ToolSpec("delete_compute", "Compute", "Delete a machine", "Delete one machine. Its boot disk goes with it; disks attached with attach_storage stay.",
             (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS, "Needs can_destroy.", "Compute Engine: compute.instances.delete."),
    # ---- storage
    ToolSpec("create_storage", "Storage", "Create storage", "Create block storage (a persistent disk, to attach to a machine) or an object bucket.",
             (REGION, Input("size_gb", "Disk size in GB; for a bucket, the expected capacity used only in the estimate.", True), PROVIDER,
              Input("type", "persistent (default): a block disk; object: a bucket."), Input("performance", "Disk class: standard, balanced (default) or ssd."), ZONE, NAME, LABELS, WAIT),
             RESPONSE, ("INVALID_STORAGE_CONFIGURATION", *COMMON_ERRORS), f"Needs can_provision. Buckets get uniform access and enforced public-access prevention. {OWNERSHIP}",
             "Compute Engine: compute.disks.insert; Cloud Storage: storage.buckets.insert."),
    ToolSpec("attach_storage", "Storage", "Attach a disk", "Attach a persistent disk to a machine in the same zone. Formatting and mounting are the Application Layer's.",
             (Input("storage_id", "The disk (sto-…).", True), Input("compute_id", "The machine (cmp-…).", True)), RESOURCE,
             ("INVALID_STORAGE_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", "PERMISSION_DENIED", "PROVIDER_ERROR", "PROVIDER_UNAVAILABLE", "OPERATION_TIMEOUT"),
             "Needs can_provision. The disk is attached without auto-delete, so deleting the machine keeps it.", "Compute Engine: compute.instances.attachDisk."),
    ToolSpec("get_storage", "Storage", "Get storage", "One disk's or bucket's live state and size.", (RESOURCE_ID,), RESOURCE, READ_ERRORS, "Read-only.",
             "Compute Engine: compute.disks.get; Cloud Storage: storage.buckets.get."),
    ToolSpec("delete_storage", "Storage", "Delete storage", "Delete one disk, or one bucket that is empty. A bucket with objects is refused: no data is destroyed by an infrastructure call.",
             (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS, "Needs can_destroy.", "Compute Engine: compute.disks.delete; Cloud Storage: storage.buckets.delete."),
    # ---- network
    ToolSpec("create_network", "Network", "Create a network", "Create a private network with one subnet in the region and the firewall rules the switches imply: internal traffic, "
             "SSH from the provider's identity-aware proxy only, and the public ports when public_access is on.",
             (REGION, PROVIDER, Input("public_access", "Allow public addresses and open allowed_ports from allowed_sources (default false)."),
              Input("cidr", "The subnet's private IPv4 range (default 10.10.0.0/24)."), Input("allowed_ports", "Ports opened from allowed_sources when public_access is on (default 80, 443)."),
              Input("allowed_sources", "IPv4 ranges allowed to reach allowed_ports (default 0.0.0.0/0)."), Input("iap_ssh", "SSH on port 22 from the identity-aware proxy range (default true)."),
              NAME, LABELS, WAIT), RESPONSE, ("INVALID_NETWORK_CONFIGURATION", *COMMON_ERRORS),
             "Needs can_provision. No rule ever opens port 22 to the internet; private ranges only for the subnet.",
             "Compute Engine: compute.networks.insert (custom subnets), compute.subnetworks.insert, compute.firewalls.insert."),
    ToolSpec("get_network", "Network", "Get a network", "A network's subnet, range, firewall rules and state.", (RESOURCE_ID,), RESOURCE, READ_ERRORS, "Read-only.",
             "Compute Engine: compute.networks.get."),
    ToolSpec("configure_firewall", "Network", "Add a firewall rule", "Add one ingress rule (TCP ports from source ranges) to a network this service created.",
             (Input("network_id", "The network (net-…).", True), Input("ports", "TCP ports to open.", True), Input("sources", "IPv4 ranges allowed in.", True)), RESOURCE,
             ("INVALID_NETWORK_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", "PERMISSION_DENIED", "PROVIDER_ERROR", "PROVIDER_UNAVAILABLE", "OPERATION_TIMEOUT"),
             "Needs can_provision. Ingress only; ports and ranges are validated.", "Compute Engine: compute.firewalls.insert."),
    ToolSpec("delete_network", "Network", "Delete a network", "Delete a network's firewall rules, service peering, subnet and the network itself, once nothing of its request runs in it.",
             (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS, "Needs can_destroy. Refused while machines or a database of the same request remain.",
             "Compute Engine: compute.firewalls.delete, compute.subnetworks.delete, compute.networks.delete; Service Networking: deleteConnection; compute.addresses.delete."),
    # ---- database
    ToolSpec("create_database", "Database", "Create a managed database", "Create one managed PostgreSQL or MySQL instance with one database and one user. The user's password is generated "
             "and returned once in this call's response; the service never stores it.",
             (REGION, PROVIDER, Input("engine", "postgresql (default) or mysql."), Input("version", "Major version (16, 8.0); the provider's default when omitted."),
              Input("size", "small (default), medium or large."), Input("storage_gb", "Storage in GB (default 10)."), Input("high_availability", "A standby in a second zone (default false)."),
              Input("public_access", "A public address instead of a private one (default false; private needs network_id)."), Input("database_name", "The database to create (default app)."),
              Input("username", "The user to create (default app)."), Input("network_id", "For a private database: a network this service created (net-…)."), NAME, LABELS, WAIT),
             RESPONSE + " Plus credentials.database_password, once.", ("INVALID_DATABASE_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", *COMMON_ERRORS),
             "Needs can_provision. Encrypted connections only, automated backups on, deletion protection on until destroy. The password is never logged or stored.",
             "Cloud SQL Admin: sqladmin.instances.insert, databases.insert, users.insert; Service Networking for a private address."),
    ToolSpec("get_database", "Database", "Get a database", "Host, port, state, version, tier and storage of one database instance.", (RESOURCE_ID,), RESOURCE, READ_ERRORS,
             "Read-only; never returns a password.", "Cloud SQL Admin: sqladmin.instances.get."),
    ToolSpec("delete_database", "Database", "Delete a database", "Turn deletion protection off and delete the instance with its data and backups.", (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS,
             "Needs can_destroy. Irreversible: the data goes with the instance.", "Cloud SQL Admin: sqladmin.instances.patch (deletion protection off), sqladmin.instances.delete."),
    # ---- cache
    ToolSpec("create_cache", "Cache", "Create a cache", "Create a managed in-memory cache (Redis) on a network this service created: private, authenticated, encrypted in transit. "
             "The Application Layer reads the AUTH string from the provider with its own identity.",
             (REGION, PRIVATE_NETWORK_ID, PROVIDER, Input("memory_gb", "Memory in GB (default 1)."), Input("high_availability", "A replica in a second zone (default false)."),
              Input("version", "Engine version (7.2); the provider's default when omitted."), NAME, LABELS, WAIT),
             RESPONSE, ("INVALID_CACHE_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", *COMMON_ERRORS), f"Needs can_provision. No credential passes through this service. {OWNERSHIP}",
             "Memorystore for Redis: redis.projects.locations.instances.create (direct peering, AUTH on, TLS)."),
    ToolSpec("get_cache", "Cache", "Get a cache", "Host, port, state, version and memory of one cache.", (RESOURCE_ID,), RESOURCE, READ_ERRORS, "Read-only; never returns the AUTH string.",
             "Memorystore for Redis: redis.projects.locations.instances.get."),
    ToolSpec("delete_cache", "Cache", "Delete a cache", "Delete one cache with its data.", (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS, "Needs can_destroy. The data goes with it.",
             "Memorystore for Redis: redis.projects.locations.instances.delete."),
    # ---- queue
    ToolSpec("create_queue", "Queue", "Create a queue", "Create a message topic and, by default, one pull subscription of the same name.",
             (REGION, Input("queue_name", "The queue's short name, part of the topic's name.", True), PROVIDER,
              Input("retention_hours", "How long unacknowledged messages are kept (default 168, up to 744)."), Input("subscription", "Create one pull subscription (default true)."),
              Input("ack_deadline_seconds", "Acknowledgement deadline (default 20)."), NAME, LABELS, WAIT),
             RESPONSE, ("NOT_FOUND", *COMMON_ERRORS), f"Needs can_provision. Publishing and pulling are the Application Layer's, with its own identity. {OWNERSHIP}",
             "Pub/Sub: pubsub.projects.topics.create, pubsub.projects.subscriptions.create."),
    ToolSpec("get_queue", "Queue", "Get a queue", "A topic's full name, its subscription and state.", (RESOURCE_ID,), RESOURCE, READ_ERRORS, "Read-only.", "Pub/Sub: pubsub.projects.topics.get."),
    ToolSpec("delete_queue", "Queue", "Delete a queue", "Delete the subscription and the topic; unread messages go with them.", (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS, "Needs can_destroy.",
             "Pub/Sub: pubsub.projects.subscriptions.delete, pubsub.projects.topics.delete."),
    # ---- secret
    ToolSpec("create_secret", "Secret", "Create a secret", "Create a secret container. Optionally write a generated random value as its first version; a value is never returned, "
             "logged or kept by this service, and the Application Layer reads and writes versions with its own identity.",
             (REGION, Input("secret_name", "The secret's short name, part of its resource name.", True), PROVIDER,
              Input("generate_value", "Write a generated random value as the first version (default false)."), NAME, LABELS, WAIT),
             RESPONSE, ("NOT_FOUND", *COMMON_ERRORS), f"Needs can_provision. No value passes through a request or a response. {OWNERSHIP}",
             "Secret Manager: secretmanager.projects.secrets.create, secretmanager.projects.secrets.addVersion."),
    ToolSpec("get_secret", "Secret", "Get a secret", "A secret's resource name, how many versions this service wrote, and its state. Never a value.", (RESOURCE_ID,), RESOURCE,
             READ_ERRORS, "Read-only; never returns a value.", "Secret Manager: secretmanager.projects.secrets.get."),
    ToolSpec("delete_secret", "Secret", "Delete a secret", "Delete the secret and every version.", (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS, "Needs can_destroy.",
             "Secret Manager: secretmanager.projects.secrets.delete."),
    # ---- cluster
    ToolSpec("create_cluster", "Cluster", "Create a Kubernetes cluster", "Create a managed Kubernetes cluster with one node pool of identical nodes: VPC-native, shielded nodes, "
             "workload identity on, versions from the provider's regular release channel. Credentials come from the provider's IAM, never from this service.",
             (REGION, PROVIDER, Input("node_count", "Nodes in the pool (default 1)."), Input("node_cpu", "Virtual CPUs per node (default 2)."),
              Input("node_memory_gb", "Memory per node in GB (default 8)."), Input("node_disk_gb", "Node disk in GB (default 50)."),
              Input("kubernetes_version", "A version the provider offers; its default channel when omitted."),
              Input("private_nodes", "Nodes without public addresses (default false)."), NETWORK_ID, ZONE, NAME, LABELS, WAIT),
             RESPONSE, ("INVALID_CLUSTER_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", *COMMON_ERRORS),
             f"Needs can_provision. Nodes get the logging, monitoring and read-only storage scopes only. {OWNERSHIP}",
             "Kubernetes Engine: container.projects.locations.clusters.create (zonal, one node pool)."),
    ToolSpec("get_cluster", "Cluster", "Get a cluster", "Endpoint, version, node count and state of one cluster.", (RESOURCE_ID,), RESOURCE, READ_ERRORS, "Read-only; never returns credentials.",
             "Kubernetes Engine: container.projects.locations.clusters.get."),
    ToolSpec("delete_cluster", "Cluster", "Delete a cluster", "Delete the cluster and its nodes; workloads and their disks go with it.", (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS,
             "Needs can_destroy.", "Kubernetes Engine: container.projects.locations.clusters.delete."),
    # ---- load balancer
    ToolSpec("create_load_balancer", "Load balancer", "Create a load balancer", "Put a public load balancer in front of machines this service created: http (an application load "
             "balancer with a health check on a path) or tcp (a passthrough network load balancer). Health checkers are allowed through the firewall on the backend port.",
             (REGION, Input("backends", "The machines behind it (cmp-…), all in one zone.", True), PROVIDER, Input("type", "http (default) or tcp."),
              Input("port", "The port the machines serve on; the public port is the same (default 80)."), Input("health_check_path", "The path an http health check requests (default /)."),
              NAME, LABELS, WAIT),
             RESPONSE, ("INVALID_LOAD_BALANCER_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", *COMMON_ERRORS),
             f"Needs can_provision. HTTP only; TLS termination is a later feature. {OWNERSHIP}",
             "Compute Engine: instance group, firewall rule, health check, backend service, URL map and target proxy (http), address and forwarding rule."),
    ToolSpec("get_load_balancer", "Load balancer", "Get a load balancer", "Public address, port, backends and state of one load balancer.", (RESOURCE_ID,), RESOURCE, READ_ERRORS,
             "Read-only.", "Compute Engine: compute.globalForwardingRules.get or compute.forwardingRules.get."),
    ToolSpec("delete_load_balancer", "Load balancer", "Delete a load balancer", "Delete every piece of the load balancer in reverse order; the machines behind it stay.",
             (RESOURCE_ID, WAIT), RESOURCE, DELETE_ERRORS, "Needs can_destroy.", "Compute Engine: the delete of each piece, forwarding rule first."),
    # ---- orchestration
    ToolSpec("list_resource_kinds", "Infrastructure", "List resource kinds", "What can be asked for: every kind of the contract with its fields, whether several fit in one request, "
             "and which configured adapters provide it. Ask this before a request for something unusual; an unknown block is refused with UNSUPPORTED_RESOURCE_KIND.",
             (), "ResourceKindsResponse: kinds (kind, description, several_per_request, fields, providers), providers, note.", (), "Read-only; no provider call.", "None."),
    ToolSpec("validate_infrastructure_request", "Infrastructure", "Validate a request", "Check a full InfrastructureRequest against the contract, the operator's regions and limits, "
             "the provider's shapes and the cost ceiling. Creates nothing.",
             (Input("request", "The InfrastructureRequest: provider, region, and at least one of network, storage, compute, database, cache, queue, secret, cluster, load_balancer.", True),),
             "ValidationResponse: valid, the normalized request, and problems (each with a code, a message and the field).", ("UNSUPPORTED_PROVIDER",),
             "Read-only. Every problem is reported, not only the first.", "None; the provider adapter is asked only for its shape rules."),
    ToolSpec("plan_infrastructure", "Infrastructure", "Plan a request", "The resources a request would create: names, provider operations, shapes, estimated monthly cost, warnings; "
             "optionally the region's quota headroom. Creates nothing.",
             (Input("request", "The InfrastructureRequest.", True), Input("check_quota", "Also read the region's quota from the provider (default false).")),
             "PlanResponse: request_id, planned resources with their provider mapping and shape, the cost estimate with its basis, warnings, quota.",
             ("UNSUPPORTED_PROVIDER", "INVALID_REGION", "INVALID_COMPUTE_CONFIGURATION", "INVALID_STORAGE_CONFIGURATION", "INVALID_NETWORK_CONFIGURATION",
              "INVALID_DATABASE_CONFIGURATION", "LIMIT_EXCEEDED", "COST_LIMIT_EXCEEDED"),
             "Read-only. The cost is an estimate from list prices, never a bill.", "Compute Engine: compute.regions.get for the quota, when asked."),
    ToolSpec("provision_infrastructure", "Infrastructure", "Provision a request", "Validate, plan, record and provision a full request: network, storage, compute, database, cache, "
             "queue, secret, cluster, load balancer, in that order. "
             "Answers at once with PROVISIONING (or waits up to 45 seconds); follow it with get_infrastructure_status. A failure deletes what was created and reports it.",
             (Input("request", "The InfrastructureRequest.", True), WAIT), RESPONSE + " With credentials.database_password, once, when a database was requested.",
             ("INVALID_COMPUTE_CONFIGURATION", "INVALID_STORAGE_CONFIGURATION", "INVALID_NETWORK_CONFIGURATION", "INVALID_DATABASE_CONFIGURATION", "NOT_FOUND", "INVALID_STATE", *COMMON_ERRORS),
             f"Needs can_provision. Refused above the limits or the cost ceiling; at most max_active_requests_per_principal live requests. {OWNERSHIP}",
             "Every operation of the primitives above, in dependency order."),
    ToolSpec("get_infrastructure_status", "Infrastructure", "Get a request's status", "A request's status, resources, endpoints and operations, each resource refreshed from the provider "
             "once provisioning is over.", (REQUEST_ID, WAIT), RESPONSE, READ_ERRORS, "Read-only.", "The get operation of every resource kind."),
    ToolSpec("list_infrastructure", "Infrastructure", "List requests", "The requests this caller may see, newest first, optionally by status.",
             (Input("status", "Only requests in this status (PROVISIONING, READY, FAILED, DESTROYING, DESTROYED, DESTROY_FAILED)."), Input("limit", "At most this many (default 50, up to 200).")),
             "ListResponse: requests and count.", ("INVALID_REQUEST",), "Read-only; from the service's own state, no provider call.", "None."),
    ToolSpec("destroy_infrastructure", "Infrastructure", "Destroy a request", "Delete everything a request created, in reverse order: load balancer, cluster, secrets, queues, "
             "cache, database, machines, storage, network. "
             "Answers at once with DESTROYING (or waits up to 45 seconds). What could not be deleted is named; call again once the cause is fixed.",
             (REQUEST_ID, WAIT), RESPONSE, DELETE_ERRORS, "Needs can_destroy. Deletion protection on a database is turned off by this call only.",
             "The delete operation of every resource kind."),
    # ---- operations
    ToolSpec("get_resource_health", "Operations", "Get a resource's health", "Whether one resource is up, from the provider's state: a running machine, a runnable database, a ready disk or network.",
             (RESOURCE_ID,), "Health: status, healthy (true, false, or null while it is still coming up) and detail.", READ_ERRORS, "Read-only.",
             "The get operation of the resource's kind."),
    ToolSpec("get_resource_usage", "Operations", "Get a resource's usage", "Utilisation over a window from the provider's monitoring: CPU for a machine; CPU, memory and disk for a database.",
             (RESOURCE_ID, Input("minutes", "The window, in minutes (default 60, 5 to 1440).")), "Usage: metrics as fractions of 1 (null where nothing was recorded) and a note.", READ_ERRORS,
             "Read-only.", "Cloud Monitoring: monitoring.timeSeries.list."),
    ToolSpec("get_resource_cost", "Operations", "Get an estimated cost", "The estimated monthly cost of a request, or of one of its resources, from list prices, with the basis of each line.",
             (Input("request_id", "The request to price."), Input("resource_id", "One resource to price instead.")),
             "request_id, resource_id, status, estimate (monthly_usd, lines with basis, source, verified, note), actual (null: no billing export).",
             ("INVALID_REQUEST", "NOT_FOUND", "PERMISSION_DENIED"), "Read-only. An estimate, never a bill.", "None; computed from the stored request and the adapter's price table."),
    ToolSpec("get_operation", "Operations", "Get an operation", "One operation's status, timing and error: a provision, destroy, cleanup, delete, resize, attach or firewall change.",
             (Input("operation_id", "The operation id from an earlier result.", True),), "OperationRecord.", ("NOT_FOUND", "PERMISSION_DENIED"), "Read-only.", "None."),
    ToolSpec("get_service_status", "Operations", "Get the service status",
             "The service's version, configured providers with their projects and allowed regions, planned providers, limits and switches.",
             (), "version, providers, planned_providers, limits, cleanup_on_failure, workers, labels.", (), "Read-only; no secret is included.", "None."),
)
BY_NAME = {spec.name: spec for spec in CATALOG}
GROUPS = ("Compute", "Storage", "Network", "Database", "Cache", "Queue", "Secret", "Cluster", "Load balancer", "Infrastructure", "Operations")


def render_markdown(catalog=CATALOG) -> str:
    """docs/MCP-CATALOG.md, from the records."""
    lines = ["# MCP service catalog", "",
             "Generated from `cloudinfra/mcp/catalog.py` by `python -m cloudinfra.cli catalog --write`; a test keeps this file and the registered tools equal to the records. "
             f"{len(catalog)} tools in {len(GROUPS)} groups. Every result is a JSON object with `status`; a failed one is "
             '`{"status": "FAILED", "error": {"code": "…", "message": "…", "details": {…}}}`. Identifiers: requests are 32 hex characters; resources are `cmp-`, `sto-`, `net-` '
             "or `db-` plus 12 hex characters. Authentication: a bearer token of a configured principal; `can_provision` for tools that create or change, `can_destroy` for "
             "tools that delete, `scope: all` to see every caller's requests.", ""]
    lines += ["## At a glance", "", "| Group | Tool | Purpose | Safety hint |", "|---|---|---|---|"]
    for group in GROUPS:
        for spec in (item for item in catalog if item.group == group):
            lines.append(f"| {group} | `{spec.name}` | {spec.title} | {spec.hint.replace('_', ' ')} |")
    lines += ["", "## Error codes", "", "| Code | Meaning |", "|---|---|"]
    lines += [f"| `{code.value}` | {MEANINGS[code]} |" for code in ErrorCode]
    lines.append("")
    for group in GROUPS:
        lines += [f"## {group}", ""]
        for spec in (item for item in catalog if item.group == group):
            lines += [f"### `{spec.name}`", "", f"**Purpose.** {spec.purpose}", ""]
            required = [item for item in spec.inputs if item.required]
            optional = [item for item in spec.inputs if not item.required]
            lines.append("**Inputs.**" + (" None." if not spec.inputs else ""))
            if spec.inputs:
                lines.append("")
                lines.append("| Field | Required | Description |")
                lines.append("|---|---|---|")
                for item in required + optional:
                    lines.append(f"| `{item.name}` | {'yes' if item.required else 'no'} | {item.description} |")
            lines += ["", f"**Required fields.** {', '.join('`' + item.name + '`' for item in required) or 'none'}.",
                      f"**Optional fields.** {', '.join('`' + item.name + '`' for item in optional) or 'none'}.", "", f"**Output.** {spec.output}", "",
                      f"**Possible errors.** {', '.join('`' + code + '`' for code in spec.errors) or 'none beyond authentication'}.", "",
                      f"**Security.** {HINT_WORDS[spec.hint]} {spec.security}", "", f"**Provider mapping (Google Cloud).** {spec.mapping}", ""]
    return "\n".join(lines).rstrip() + "\n"
