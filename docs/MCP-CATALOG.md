# MCP service catalog

Generated from `cloudinfra/mcp/catalog.py` by `python -m cloudinfra.cli catalog --write`; a test keeps this file and the registered tools equal to the records. 45 tools in 11 groups. Every result is a JSON object with `status`; a failed one is `{"status": "FAILED", "error": {"code": "…", "message": "…", "details": {…}}}`. Identifiers: requests are 32 hex characters; resources are `cmp-`, `sto-`, `net-`, `db-`, `cac-`, `que-`, `sec-`, `clu-` or `lb-` plus 12 hex characters. Authentication: a bearer token of a configured principal; `can_provision` for tools that create or change, `can_destroy` for tools that delete, `scope: all` to see every caller's requests.

## At a glance

| Group | Tool | Purpose | Safety hint |
|---|---|---|---|
| Compute | `create_compute` | Create virtual machines | changes cloud |
| Compute | `get_compute` | Get a machine | read only |
| Compute | `list_compute` | List machines | read only |
| Compute | `resize_compute` | Resize a machine | changes cloud |
| Compute | `delete_compute` | Delete a machine | destructive |
| Storage | `create_storage` | Create storage | changes cloud |
| Storage | `attach_storage` | Attach a disk | changes cloud |
| Storage | `get_storage` | Get storage | read only |
| Storage | `delete_storage` | Delete storage | destructive |
| Network | `create_network` | Create a network | changes cloud |
| Network | `get_network` | Get a network | read only |
| Network | `configure_firewall` | Add a firewall rule | changes cloud |
| Network | `delete_network` | Delete a network | destructive |
| Database | `create_database` | Create a managed database | changes cloud |
| Database | `get_database` | Get a database | read only |
| Database | `delete_database` | Delete a database | destructive |
| Cache | `create_cache` | Create a cache | changes cloud |
| Cache | `get_cache` | Get a cache | read only |
| Cache | `delete_cache` | Delete a cache | destructive |
| Queue | `create_queue` | Create a queue | changes cloud |
| Queue | `get_queue` | Get a queue | read only |
| Queue | `delete_queue` | Delete a queue | destructive |
| Secret | `create_secret` | Create a secret | changes cloud |
| Secret | `get_secret` | Get a secret | read only |
| Secret | `delete_secret` | Delete a secret | destructive |
| Cluster | `create_cluster` | Create a Kubernetes cluster | changes cloud |
| Cluster | `get_cluster` | Get a cluster | read only |
| Cluster | `delete_cluster` | Delete a cluster | destructive |
| Load balancer | `create_load_balancer` | Create a load balancer | changes cloud |
| Load balancer | `get_load_balancer` | Get a load balancer | read only |
| Load balancer | `delete_load_balancer` | Delete a load balancer | destructive |
| Infrastructure | `list_resource_kinds` | List resource kinds | read only |
| Infrastructure | `validate_infrastructure_request` | Validate a request | read only |
| Infrastructure | `plan_infrastructure` | Plan a request | read only |
| Infrastructure | `provision_infrastructure` | Provision a request | changes cloud |
| Infrastructure | `get_infrastructure_status` | Get a request's status | read only |
| Infrastructure | `list_infrastructure` | List requests | read only |
| Infrastructure | `destroy_infrastructure` | Destroy a request | destructive |
| Infrastructure | `control_infrastructure` | Stop, start or extend a request | changes cloud |
| Infrastructure | `manage_infrastructure_access` | Grant or revoke access | changes cloud |
| Operations | `get_resource_health` | Get a resource's health | read only |
| Operations | `get_resource_usage` | Get a resource's usage | read only |
| Operations | `get_resource_cost` | Get an estimated cost | read only |
| Operations | `get_operation` | Get an operation | read only |
| Operations | `get_service_status` | Get the service status | read only |

## Error codes

| Code | Meaning |
|---|---|
| `INVALID_REQUEST` | The request does not fit the contract: a missing or unknown field, a wrong type, nothing asked for. |
| `UNSUPPORTED_PROVIDER` | No adapter for that provider name, or the operator has not configured it. |
| `INVALID_REGION` | Not a region, not allowed by the operator, or a zone outside it. |
| `INVALID_RESOURCE_NAME` | A name outside the lowercase-letters-digits-dashes rule. |
| `INVALID_COMPUTE_CONFIGURATION` | A machine shape the provider has no type for. |
| `INVALID_STORAGE_CONFIGURATION` | A storage request the provider cannot satisfy (attaching a bucket, a disk across zones). |
| `INVALID_NETWORK_CONFIGURATION` | A range, port or source outside the rules, or a public address without a public network. |
| `INVALID_DATABASE_CONFIGURATION` | An engine version the provider does not run, or a private database without a network. |
| `INVALID_CACHE_CONFIGURATION` | A cache version the provider does not run, or a cache without a network. |
| `INVALID_CLUSTER_CONFIGURATION` | A node shape the provider has no type for. |
| `INVALID_LOAD_BALANCER_CONFIGURATION` | No machines behind it, or machines in different zones or regions. |
| `UNSUPPORTED_RESOURCE_KIND` | A request block of a kind the contract or the chosen adapter does not provide; details name the nearest kind. |
| `INVALID_ACCESS` | A member outside the operator's allowed domains, a malformed member, a duplicate, or more members than allowed. |
| `LIMIT_EXCEEDED` | Above one of the operator's limits on counts, sizes or live requests. |
| `COST_LIMIT_EXCEEDED` | The estimate from list prices is above the ceiling for this service or this caller. |
| `QUOTA_EXCEEDED` | The provider's quota in the region has no room for the request. |
| `NOT_FOUND` | No such request, resource or operation is visible to this caller. |
| `PERMISSION_DENIED` | The caller may not do this (can_provision, can_destroy), or did not authenticate. |
| `INVALID_STATE` | The resource or request is not in a state that allows this (still provisioning, already destroyed, a network in use). |
| `PROVIDER_AUTH_FAILED` | The service could not get or use its own provider credential. |
| `PROVIDER_PERMISSION_DENIED` | The service's identity lacks a permission at the provider. |
| `PROVIDER_CONFLICT` | The provider reports a conflict: a name in use, a bucket with objects, a precondition failed. |
| `PROVIDER_UNAVAILABLE` | The provider could not be reached, is rate limiting, or answered 5xx; retry later. |
| `PROVIDER_ERROR` | The provider refused the request for another reason; its message is in details. |
| `OPERATION_TIMEOUT` | A provider operation did not finish within operation_timeout_seconds; check the status. |
| `PROVISIONING_FAILED` | A request failed while provisioning; details name the cause and what cleanup did. |
| `DESTROY_FAILED` | Some resources could not be deleted; details name them, and the call can be repeated. |
| `INTERNAL_ERROR` | The service stopped on an unexpected exception; nothing was retried. |

## Compute

### `create_compute`

**Purpose.** Create one or more identical virtual machines: CPUs, memory, an operating system family or an operator profile, boot disk, optional GPUs and public address, and optionally when they stop by themselves. No image, script, key or command is taken; access is through the provider's IAM-based login.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `cpu` | yes | Virtual CPUs per machine. |
| `memory_gb` | yes | Memory per machine, in GB. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `count` | no | How many identical machines (default 1). |
| `os` | no | ubuntu (the default without a profile), debian or rocky. |
| `profile` | no | A software profile the operator defines, such as a GPU image with signed drivers; list_resource_kinds names them. Instead of os. |
| `boot_disk_gb` | no | Boot disk in GB (default 20; 50 with GPUs, which need at least 40). |
| `public_ip` | no | Give each machine a public address (default false). |
| `network_id` | no | A network this service created earlier (net-…), to join instead of the operator's default network. |
| `accelerator` | no | GPUs per machine: t4, l4, a100 or h100; the machine family follows the type. Drivers come with a GPU profile (accelerator: required); on any other image the Application Layer installs them. |
| `accelerator_count` | no | GPUs per machine (default 1); each type offers fixed counts. |
| `autostop_idle_minutes` | no | Stop a machine after this many idle minutes (15 to 1440); the operator may set a default for machines with GPUs. |
| `max_run_hours` | no | Stop each machine after it has run this many hours since its last start (1 to 2880). |
| `lifetime_hours` | no | Hours until the request expires and stops (1 to 8760); the operator's default when omitted, if there is one. A lifetime ends in a destroy, so it needs can_destroy. |
| `zone` | no | A zone of the region; the provider picks the first when omitted. |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`, `cpu`, `memory_gb`.
**Optional fields.** `provider`, `count`, `os`, `profile`, `boot_disk_gb`, `public_ip`, `network_id`, `accelerator`, `accelerator_count`, `autostop_idle_minutes`, `max_run_hours`, `lifetime_hours`, `zone`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `INVALID_COMPUTE_CONFIGURATION`, `INVALID_NETWORK_CONFIGURATION`, `NOT_FOUND`, `INVALID_STATE`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision, and can_destroy with lifetime_hours. Machines get OS Login (IAM) and no service account; vTPM and integrity monitoring on, and Secure Boot on unless GPUs run an image without their drivers, whose driver installation needs it off. The requester is granted login when the operator maps it to an identity. Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own.

**Provider mapping (Google Cloud).** Compute Engine: compute.instances.insert with an E2 machine type (N1, G2, A2 or A3 with a GPU), a public image family or the profile's image, and scheduling.maxRunDuration with instanceTerminationAction STOP for max_run_hours; compute.instances.get after.

### `get_compute`

**Purpose.** One machine's live state: status, addresses, machine type, attached disks.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only; the caller sees its own requests' resources (scope: all sees every one).

**Provider mapping (Google Cloud).** Compute Engine: compute.instances.get.

### `list_compute`

**Purpose.** Every machine this service created, as the provider sees them now, optionally for one request.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request_id` | no | Only the machines of this request. |

**Required fields.** none.
**Optional fields.** `request_id`.

**Output.** instances: a list of machines with their addresses and states; count; the labels used to select them.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only; selects by the ownership labels, so a caller with scope own sees its own machines only.

**Provider mapping (Google Cloud).** Compute Engine: compute.instances.aggregatedList with a label filter.

### `resize_compute`

**Purpose.** Change a machine's CPUs and memory: a running machine is stopped, resized and started again; a stopped one is resized and stays stopped. A machine with GPUs keeps them and their machine family, where a family of fixed shapes gives the smallest that covers what is asked. Its addresses stay unless the provider reassigns an ephemeral one.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `cpu` | yes | The new number of virtual CPUs. |
| `memory_gb` | yes | The new memory, in GB. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`, `cpu`, `memory_gb`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `INVALID_COMPUTE_CONFIGURATION`, `NOT_FOUND`, `INVALID_STATE`, `LIMIT_EXCEEDED`, `PERMISSION_DENIED`, `PROVIDER_ERROR`, `PROVIDER_UNAVAILABLE`, `OPERATION_TIMEOUT`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. A running machine is unavailable while it restarts; refused while its request is stopping, starting or otherwise changing, and refused before the machine is touched when its family cannot take the shape.

**Provider mapping (Google Cloud).** Compute Engine: compute.instances.stop, setMachineType, start.

### `delete_compute`

**Purpose.** Delete one machine. Its boot disk goes with it; disks attached with attach_storage stay.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy.

**Provider mapping (Google Cloud).** Compute Engine: compute.instances.delete.

## Storage

### `create_storage`

**Purpose.** Create block storage (a persistent disk, to attach to a machine) or an object bucket.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `size_gb` | yes | Disk size in GB; for a bucket, the expected capacity used only in the estimate. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `type` | no | persistent (default): a block disk; object: a bucket. |
| `performance` | no | Disk class: standard, balanced (default) or ssd. |
| `zone` | no | A zone of the region; the provider picks the first when omitted. |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`, `size_gb`.
**Optional fields.** `provider`, `type`, `performance`, `zone`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `INVALID_STORAGE_CONFIGURATION`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. Buckets get uniform access and enforced public-access prevention. Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own.

**Provider mapping (Google Cloud).** Compute Engine: compute.disks.insert; Cloud Storage: storage.buckets.insert.

### `attach_storage`

**Purpose.** Attach a persistent disk to a machine in the same zone. Formatting and mounting are the Application Layer's.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `storage_id` | yes | The disk (sto-…). |
| `compute_id` | yes | The machine (cmp-…). |

**Required fields.** `storage_id`, `compute_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `INVALID_STORAGE_CONFIGURATION`, `NOT_FOUND`, `INVALID_STATE`, `PERMISSION_DENIED`, `PROVIDER_ERROR`, `PROVIDER_UNAVAILABLE`, `OPERATION_TIMEOUT`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. The disk is attached without auto-delete, so deleting the machine keeps it.

**Provider mapping (Google Cloud).** Compute Engine: compute.instances.attachDisk.

### `get_storage`

**Purpose.** One disk's or bucket's live state and size.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only.

**Provider mapping (Google Cloud).** Compute Engine: compute.disks.get; Cloud Storage: storage.buckets.get.

### `delete_storage`

**Purpose.** Delete one disk, or one bucket that is empty. A bucket with objects is refused: no data is destroyed by an infrastructure call.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy.

**Provider mapping (Google Cloud).** Compute Engine: compute.disks.delete; Cloud Storage: storage.buckets.delete.

## Network

### `create_network`

**Purpose.** Create a private network with one subnet in the region and the firewall rules the switches imply: internal traffic, SSH from the provider's identity-aware proxy only, and the public ports when public_access is on.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `public_access` | no | Allow public addresses and open allowed_ports from allowed_sources (default false). |
| `cidr` | no | The subnet's private IPv4 range (default 10.10.0.0/24). |
| `allowed_ports` | no | Ports opened from allowed_sources when public_access is on (default 80, 443). |
| `allowed_sources` | no | IPv4 ranges allowed to reach allowed_ports (default 0.0.0.0/0). |
| `iap_ssh` | no | SSH on port 22 from the identity-aware proxy range (default true). |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`.
**Optional fields.** `provider`, `public_access`, `cidr`, `allowed_ports`, `allowed_sources`, `iap_ssh`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `INVALID_NETWORK_CONFIGURATION`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. No rule ever opens port 22 to the internet; private ranges only for the subnet.

**Provider mapping (Google Cloud).** Compute Engine: compute.networks.insert (custom subnets), compute.subnetworks.insert, compute.firewalls.insert.

### `get_network`

**Purpose.** A network's subnet, range, firewall rules and state.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only.

**Provider mapping (Google Cloud).** Compute Engine: compute.networks.get.

### `configure_firewall`

**Purpose.** Add one ingress rule (TCP ports from source ranges) to a network this service created.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `network_id` | yes | The network (net-…). |
| `ports` | yes | TCP ports to open. |
| `sources` | yes | IPv4 ranges allowed in. |

**Required fields.** `network_id`, `ports`, `sources`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `INVALID_NETWORK_CONFIGURATION`, `NOT_FOUND`, `INVALID_STATE`, `PERMISSION_DENIED`, `PROVIDER_ERROR`, `PROVIDER_UNAVAILABLE`, `OPERATION_TIMEOUT`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. Ingress only; ports and ranges are validated.

**Provider mapping (Google Cloud).** Compute Engine: compute.firewalls.insert.

### `delete_network`

**Purpose.** Delete a network's firewall rules, service peering, subnet and the network itself, once nothing of its request runs in it.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy. Refused while machines or a database of the same request remain.

**Provider mapping (Google Cloud).** Compute Engine: compute.firewalls.delete, compute.subnetworks.delete, compute.networks.delete; Service Networking: deleteConnection; compute.addresses.delete.

## Database

### `create_database`

**Purpose.** Create one managed PostgreSQL or MySQL instance with one database and one user. The user's password is generated and written to a secret the request owns (db-password), which the request's access members read; a response carries it only if the operator allows that.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `engine` | no | postgresql (default) or mysql. |
| `version` | no | Major version (16, 8.0); the provider's default when omitted. |
| `size` | no | small (default), medium or large. |
| `storage_gb` | no | Storage in GB (default 10). |
| `high_availability` | no | A standby in a second zone (default false). |
| `public_access` | no | A public address instead of a private one (default false; private needs network_id). |
| `database_name` | no | The database to create (default app). |
| `username` | no | The user to create (default app). |
| `network_id` | no | For a private database: a network this service created (net-…). |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`.
**Optional fields.** `provider`, `engine`, `version`, `size`, `storage_gb`, `high_availability`, `public_access`, `database_name`, `username`, `network_id`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED. Plus credentials naming the password's secret.

**Possible errors.** `INVALID_DATABASE_CONFIGURATION`, `NOT_FOUND`, `INVALID_STATE`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. Encrypted connections only, automated backups on, deletion protection on until destroy. The password is never logged or kept by this service.

**Provider mapping (Google Cloud).** Cloud SQL Admin: sqladmin.instances.insert, databases.insert, users.insert; Service Networking for a private address; Secret Manager for the password.

### `get_database`

**Purpose.** Host, port, state, version, tier and storage of one database instance.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only; never returns a password.

**Provider mapping (Google Cloud).** Cloud SQL Admin: sqladmin.instances.get.

### `delete_database`

**Purpose.** Turn deletion protection off and delete the instance with its data and backups, and the secret that holds its password.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy. Irreversible: the data goes with the instance.

**Provider mapping (Google Cloud).** Cloud SQL Admin: sqladmin.instances.patch (deletion protection off), sqladmin.instances.delete; Secret Manager for the password's secret.

## Cache

### `create_cache`

**Purpose.** Create a managed in-memory cache (Redis) on a network this service created: private, authenticated, encrypted in transit. Its AUTH string is copied into a secret the request owns (cache-auth), which the request's access members read.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `network_id` | yes | A network this service created earlier (net-…); required, since the resource is private. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `memory_gb` | no | Memory in GB (default 1). |
| `high_availability` | no | A replica in a second zone (default false). |
| `version` | no | Engine version (7.2); the provider's default when omitted. |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`, `network_id`.
**Optional fields.** `provider`, `memory_gb`, `high_availability`, `version`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `INVALID_CACHE_CONFIGURATION`, `NOT_FOUND`, `INVALID_STATE`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. The AUTH string goes from the provider into the request's secret and nowhere else; it is never returned or logged. Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own.

**Provider mapping (Google Cloud).** Memorystore for Redis: redis.projects.locations.instances.create (direct peering, AUTH on, TLS) and instances.getAuthString; Secret Manager for the copy.

### `get_cache`

**Purpose.** Host, port, state, version and memory of one cache.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only; never returns the AUTH string.

**Provider mapping (Google Cloud).** Memorystore for Redis: redis.projects.locations.instances.get.

### `delete_cache`

**Purpose.** Delete one cache with its data, and the secret that holds its AUTH string.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy. The data goes with it.

**Provider mapping (Google Cloud).** Memorystore for Redis: redis.projects.locations.instances.delete; Secret Manager for the AUTH string's secret.

## Queue

### `create_queue`

**Purpose.** Create a message topic and, by default, one pull subscription of the same name.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `queue_name` | yes | The queue's short name, part of the topic's name. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `retention_hours` | no | How long unacknowledged messages are kept (default 168, up to 744). |
| `subscription` | no | Create one pull subscription (default true). |
| `ack_deadline_seconds` | no | Acknowledgement deadline (default 20). |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`, `queue_name`.
**Optional fields.** `provider`, `retention_hours`, `subscription`, `ack_deadline_seconds`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. Publishing and pulling are the Application Layer's, with its own identity. Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own.

**Provider mapping (Google Cloud).** Pub/Sub: pubsub.projects.topics.create, pubsub.projects.subscriptions.create.

### `get_queue`

**Purpose.** A topic's full name, its subscription and state.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only.

**Provider mapping (Google Cloud).** Pub/Sub: pubsub.projects.topics.get.

### `delete_queue`

**Purpose.** Delete the subscription and the topic; unread messages go with them.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy.

**Provider mapping (Google Cloud).** Pub/Sub: pubsub.projects.subscriptions.delete, pubsub.projects.topics.delete.

## Secret

### `create_secret`

**Purpose.** Create a secret container. Optionally write a generated random value as its first version; a value is never returned, logged or kept by this service, and the Application Layer reads and writes versions with its own identity.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `secret_name` | yes | The secret's short name, part of its resource name. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `generate_value` | no | Write a generated random value as the first version (default false). |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`, `secret_name`.
**Optional fields.** `provider`, `generate_value`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. No value passes through a request or a response. Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own.

**Provider mapping (Google Cloud).** Secret Manager: secretmanager.projects.secrets.create, secretmanager.projects.secrets.addVersion.

### `get_secret`

**Purpose.** A secret's resource name, how many versions this service wrote, and its state. Never a value.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only; never returns a value.

**Provider mapping (Google Cloud).** Secret Manager: secretmanager.projects.secrets.get.

### `delete_secret`

**Purpose.** Delete the secret and every version.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy.

**Provider mapping (Google Cloud).** Secret Manager: secretmanager.projects.secrets.delete.

## Cluster

### `create_cluster`

**Purpose.** Create a managed Kubernetes cluster with one node pool of identical nodes: VPC-native, shielded nodes, workload identity on, versions from the provider's regular release channel. Credentials come from the provider's IAM, never from this service.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `node_count` | no | Nodes in the pool (default 1). |
| `node_cpu` | no | Virtual CPUs per node (default 2). |
| `node_memory_gb` | no | Memory per node in GB (default 8). |
| `node_disk_gb` | no | Node disk in GB (default 50). |
| `kubernetes_version` | no | A version the provider offers; its default channel when omitted. |
| `private_nodes` | no | Nodes without public addresses (default false). |
| `network_id` | no | A network this service created earlier (net-…), to join instead of the operator's default network. |
| `registries` | no | Artifact Registry repositories (LOCATION-docker.pkg.dev/PROJECT/REPO) the nodes may pull images from; only ones the operator lists. |
| `public` | no | A front door: a reserved public address and a Gateway taking routes from the request's namespace (default false). |
| `hostnames` | no | The front door's DNS names, in domains the operator allows: HTTPS with a Google-managed certificate; none, plain HTTP. |
| `zone` | no | A zone of the region; the provider picks the first when omitted. |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`.
**Optional fields.** `provider`, `node_count`, `node_cpu`, `node_memory_gb`, `node_disk_gb`, `kubernetes_version`, `private_nodes`, `network_id`, `registries`, `public`, `hostnames`, `zone`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `INVALID_CLUSTER_CONFIGURATION`, `NOT_FOUND`, `INVALID_STATE`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. The nodes run as a service account made for the cluster, with the minimum node role on the project and reader on the named registries only, deleted with the cluster; they get the logging, monitoring and read-only storage scopes. Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own.

**Provider mapping (Google Cloud).** Kubernetes Engine: container.projects.locations.clusters.create (zonal, one node pool); IAM: serviceAccounts.create and delete, and the project's setIamPolicy for roles/container.defaultNodeServiceAccount only; Artifact Registry: repositories setIamPolicy; for a front door Compute Engine globalAddresses and sslCertificates insert and delete, and the cluster's Gateway API.

### `get_cluster`

**Purpose.** Endpoint, version, node count and state of one cluster.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only; never returns credentials.

**Provider mapping (Google Cloud).** Kubernetes Engine: container.projects.locations.clusters.get.

### `delete_cluster`

**Purpose.** Delete the cluster and its nodes; workloads and their disks go with it.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy.

**Provider mapping (Google Cloud).** Kubernetes Engine: container.projects.locations.clusters.delete.

## Load balancer

### `create_load_balancer`

**Purpose.** Put a public load balancer in front of machines this service created: http (an application load balancer with a health check on a path) or tcp (a passthrough network load balancer). Health checkers are allowed through the firewall on the backend port.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `region` | yes | The provider's region (us-central1); must be in the service's allowed regions. |
| `backends` | yes | The machines behind it (cmp-…), all in one zone. |
| `provider` | no | The cloud provider adapter; gcp (default) is the one implemented. |
| `type` | no | http (default) or tcp. |
| `port` | no | The port the machines serve on; the public port is the same (default 80). |
| `health_check_path` | no | The path an http health check requests (default /). |
| `name` | no | A short name (1 to 20 lowercase letters, digits, dashes) used as the prefix of every resource name; infra-<8 hex> when omitted. |
| `labels` | no | Labels for every resource, next to the service's ownership labels. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `region`, `backends`.
**Optional fields.** `provider`, `type`, `port`, `health_check_path`, `name`, `labels`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `INVALID_LOAD_BALANCER_CONFIGURATION`, `NOT_FOUND`, `INVALID_STATE`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. HTTP only; TLS termination is a later feature. Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own.

**Provider mapping (Google Cloud).** Compute Engine: instance group, firewall rule, health check, backend service, URL map and target proxy (http), address and forwarding rule.

### `get_load_balancer`

**Purpose.** Public address, port, backends and state of one load balancer.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only.

**Provider mapping (Google Cloud).** Compute Engine: compute.globalForwardingRules.get or compute.forwardingRules.get.

### `delete_load_balancer`

**Purpose.** Delete every piece of the load balancer in reverse order; the machines behind it stay.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `resource_id`.
**Optional fields.** `wait_seconds`.

**Output.** ResourceResponse: status, request_id, the resource as the provider sees it now, and the running operation when there is one.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy.

**Provider mapping (Google Cloud).** Compute Engine: the delete of each piece, forwarding rule first.

## Infrastructure

### `list_resource_kinds`

**Purpose.** What can be asked for: every kind of the contract with its fields, whether several fit in one request, and which configured adapters provide it. Ask this before a request for something unusual; an unknown block is refused with UNSUPPORTED_RESOURCE_KIND.

**Inputs.** None.

**Required fields.** none.
**Optional fields.** none.

**Output.** ResourceKindsResponse: kinds (kind, description, several_per_request, fields, providers), providers, profiles (each provider's software profiles), note.

**Possible errors.** none beyond authentication.

**Security.** Read-only; creates and changes nothing. Read-only; no provider call.

**Provider mapping (Google Cloud).** None.

### `validate_infrastructure_request`

**Purpose.** Check a full InfrastructureRequest against the contract, the operator's regions and limits, the provider's shapes and the cost ceiling. Creates nothing.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request` | yes | The InfrastructureRequest: provider, region, at least one of network, storage, compute, database, cache, queue, secret, cluster, load_balancer, and optionally lifetime (hours, on_expiry) and access (members to grant). |

**Required fields.** `request`.
**Optional fields.** none.

**Output.** ValidationResponse: valid, the normalized request, and problems (each with a code, a message and the field).

**Possible errors.** `UNSUPPORTED_PROVIDER`.

**Security.** Read-only; creates and changes nothing. Read-only. Every problem is reported, not only the first.

**Provider mapping (Google Cloud).** None; the provider adapter is asked only for its shape rules.

### `plan_infrastructure`

**Purpose.** The resources a request would create: names, provider operations, shapes, estimated monthly cost, warnings; optionally the region's quota headroom. Creates nothing.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request` | yes | The InfrastructureRequest. |
| `check_quota` | no | Also read the region's quota from the provider (default false). |

**Required fields.** `request`.
**Optional fields.** `check_quota`.

**Output.** PlanResponse: request_id, planned resources with their provider mapping and shape, the cost estimate with its basis, warnings, quota.

**Possible errors.** `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_COMPUTE_CONFIGURATION`, `INVALID_STORAGE_CONFIGURATION`, `INVALID_NETWORK_CONFIGURATION`, `INVALID_DATABASE_CONFIGURATION`, `INVALID_ACCESS`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`.

**Security.** Read-only; creates and changes nothing. Read-only. The cost is an estimate from list prices, never a bill; stopped_monthly_usd is what it costs while stopped.

**Provider mapping (Google Cloud).** Compute Engine: compute.regions.get for the quota, when asked.

### `provision_infrastructure`

**Purpose.** Validate, plan, record and provision a full request: network, storage, compute, database, cache, queue, secret, cluster, load balancer, in that order. Answers at once with PROVISIONING (or waits up to 45 seconds); follow it with get_infrastructure_status. A failure deletes what was created and reports it.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request` | yes | The InfrastructureRequest. |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `request`.
**Optional fields.** `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED. With credentials naming the secrets that hold a database password or a cache AUTH string.

**Possible errors.** `INVALID_COMPUTE_CONFIGURATION`, `INVALID_STORAGE_CONFIGURATION`, `INVALID_NETWORK_CONFIGURATION`, `INVALID_DATABASE_CONFIGURATION`, `INVALID_ACCESS`, `NOT_FOUND`, `INVALID_STATE`, `PERMISSION_DENIED`, `UNSUPPORTED_PROVIDER`, `INVALID_REGION`, `INVALID_RESOURCE_NAME`, `LIMIT_EXCEEDED`, `COST_LIMIT_EXCEEDED`, `QUOTA_EXCEEDED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `PROVISIONING_FAILED`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision, and can_destroy for a request with a lifetime, which ends in a destroy. Refused above the limits or the cost ceiling; at most max_active_requests_per_principal live requests. Access members are granted on the resources once they exist, never on the project; a grant that fails is reported on the member, not as a failed request. Every resource carries labels managed-by=cloudinfra, cloudinfra-request, cloudinfra-name and cloudinfra-owner, plus the operator's and the request's own.

**Provider mapping (Google Cloud).** Every operation of the primitives above, in dependency order.

### `get_infrastructure_status`

**Purpose.** A request's status, resources, endpoints and operations, each resource refreshed from the provider once provisioning is over.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request_id` | yes | The request id this service issued (32 hex characters). |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `request_id`.
**Optional fields.** `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only.

**Provider mapping (Google Cloud).** The get operation of every resource kind.

### `list_infrastructure`

**Purpose.** The requests this caller may see, newest first, optionally by status or by how soon they expire.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `status` | no | Only requests in this status (PROVISIONING, READY, STOPPING, STOPPED, STARTING, FAILED, DESTROYING, DESTROYED, DESTROY_FAILED). |
| `limit` | no | At most this many (default 50, up to 200). |
| `expiring_within_hours` | no | Only running or stopped requests that expire within this many hours (1 to 8760), the expired ones included. |

**Required fields.** none.
**Optional fields.** `status`, `limit`, `expiring_within_hours`.

**Output.** ListResponse: requests and count.

**Possible errors.** `INVALID_REQUEST`.

**Security.** Read-only; creates and changes nothing. Read-only; from the service's own state, no provider call.

**Provider mapping (Google Cloud).** None.

### `destroy_infrastructure`

**Purpose.** Delete everything a request created, in reverse order: load balancer, cluster, secrets, queues, cache, database, machines, storage, network. Answers at once with DESTROYING (or waits up to 45 seconds). What could not be deleted is named; call again once the cause is fixed.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request_id` | yes | The request id this service issued (32 hex characters). |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `request_id`.
**Optional fields.** `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `INVALID_STATE`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`, `DESTROY_FAILED`.

**Security.** Deletes cloud resources; needs can_destroy. Needs can_destroy. Deletion protection on a database is turned off by this call only.

**Provider mapping (Google Cloud).** The delete operation of every resource kind.

### `control_infrastructure`

**Purpose.** Stop a READY request's machines and databases, start them again, move its expiry later by hours (up to the operator's maximum lifetime), or keep its machines awake: idle stop waits for hours. While stopped, disks, addresses and what cannot stop keep billing; the lifecycle block names them.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request_id` | yes | The request id this service issued (32 hex characters). |
| `action` | yes | stop, start, extend or keep_awake. |
| `hours` | no | For extend: hours added to the expiry. For keep_awake: how long idle stop waits (up to the operator's max_keep_awake_hours). |
| `wait_seconds` | no | Wait up to this many seconds (0 to 45) for the outcome before answering; 0 answers at once with the operation running. |

**Required fields.** `request_id`, `action`.
**Optional fields.** `hours`, `wait_seconds`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED.

**Possible errors.** `INVALID_REQUEST`, `INVALID_STATE`, `LIMIT_EXCEEDED`, `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`, `OPERATION_TIMEOUT`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. An expired request is started only after an extend; a request that is stopping or starting is extended once it has finished. Nothing stops by itself unless the request or the operator asked for it.

**Provider mapping (Google Cloud).** Compute Engine: compute.instances.stop and start; Cloud SQL Admin: sqladmin.instances.patch with activationPolicy NEVER or ALWAYS.

### `manage_infrastructure_access`

**Purpose.** Grant a person, group or service account access to what a request created, or revoke it: login on its machines through the provider's IAM and an identity-aware tunnel, reading its secrets (the database password and cache AUTH string among them), using its buckets and queues.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request_id` | yes | The request id this service issued (32 hex characters). |
| `action` | yes | grant or revoke. |
| `member` | yes | user:, group: or serviceAccount: and an email address in a domain the operator allows, or a principal's own identity. |
| `level` | no | use (default: log in, read and write data) or admin (administrator login, manage secret versions and objects). |

**Required fields.** `request_id`, `action`, `member`.
**Optional fields.** `level`.

**Output.** InfrastructureResponse: request_id, status, the resources with ids, names, addresses, states and connect blocks (commands, prerequisites), endpoints, lifecycle (expiry, stop reason, what keeps billing while stopped), access (each member's grants), warnings, operations, the cost estimate, and error when FAILED. The access block shows each member's status (RECORDED when nothing in the request has an access policy) and the resources granted on; each resource's connect block says how to reach it.

**Possible errors.** `INVALID_REQUEST`, `INVALID_ACCESS`, `INVALID_STATE`, `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_CONFLICT`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Creates or changes cloud resources, which can cost money; needs can_provision. Needs can_provision. Grants go on each resource's own policy and never on the project; what a member needs at project level is named in each connect block as a prerequisite for the platform team. Members are limited to the operator's allowed domains, principals' identities and max_members. A grant needs a READY or STOPPED request; a revoke also reaches what a failed request or a failed destroy left behind.

**Provider mapping (Google Cloud).** Compute Engine: instances getIamPolicy and setIamPolicy (roles/compute.osLogin or osAdminLogin); IAP: the machine's iap_tunnel policy (roles/iap.tunnelResourceAccessor); Secret Manager, Cloud Storage and Pub/Sub: the resource's getIamPolicy and setIamPolicy.

## Operations

### `get_resource_health`

**Purpose.** Whether one resource is up, from the provider's state: a running machine, a runnable database, a ready disk or network.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |

**Required fields.** `resource_id`.
**Optional fields.** none.

**Output.** Health: status, healthy (true, false, or null while it is still coming up) and detail.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only.

**Provider mapping (Google Cloud).** The get operation of the resource's kind.

### `get_resource_usage`

**Purpose.** Utilisation over a window from the provider's monitoring: CPU for a machine; CPU, memory and disk for a database.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `resource_id` | yes | The resource id this service issued. |
| `minutes` | no | The window, in minutes (default 60, 5 to 1440). |

**Required fields.** `resource_id`.
**Optional fields.** `minutes`.

**Output.** Usage: metrics as fractions of 1 (null where nothing was recorded) and a note.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`, `PROVIDER_AUTH_FAILED`, `PROVIDER_PERMISSION_DENIED`, `PROVIDER_UNAVAILABLE`, `PROVIDER_ERROR`.

**Security.** Read-only; creates and changes nothing. Read-only.

**Provider mapping (Google Cloud).** Cloud Monitoring: monitoring.timeSeries.list.

### `get_resource_cost`

**Purpose.** The estimated monthly cost of a request, or of one of its resources, from list prices, with the basis of each line.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `request_id` | no | The request to price. |
| `resource_id` | no | One resource to price instead. |

**Required fields.** none.
**Optional fields.** `request_id`, `resource_id`.

**Output.** request_id, resource_id, status, estimate (monthly_usd, stopped_monthly_usd, lines with basis, source, verified, note), current_monthly_usd (the stopped rate while stopped), actual (null: no billing export).

**Possible errors.** `INVALID_REQUEST`, `NOT_FOUND`, `PERMISSION_DENIED`.

**Security.** Read-only; creates and changes nothing. Read-only. An estimate, never a bill.

**Provider mapping (Google Cloud).** None; computed from the stored request and the adapter's price table.

### `get_operation`

**Purpose.** One operation's status, timing and error: a provision, destroy, cleanup, delete, resize, attach, firewall change, stop, start, extend, keep_awake or access change.

**Inputs.**

| Field | Required | Description |
|---|---|---|
| `operation_id` | yes | The operation id from an earlier result. |

**Required fields.** `operation_id`.
**Optional fields.** none.

**Output.** OperationRecord.

**Possible errors.** `NOT_FOUND`, `PERMISSION_DENIED`.

**Security.** Read-only; creates and changes nothing. Read-only.

**Provider mapping (Google Cloud).** None.

### `get_service_status`

**Purpose.** The service's version, configured providers with their projects and allowed regions, planned providers, limits (lifetime and idle stop among them), access rules, software profiles and switches.

**Inputs.** None.

**Required fields.** none.
**Optional fields.** none.

**Output.** version, providers, planned_providers, limits, cleanup_on_failure, workers, labels, access, sweep_interval_seconds, return_database_password, profiles.

**Possible errors.** none beyond authentication.

**Security.** Read-only; creates and changes nothing. Read-only; no secret is included.

**Provider mapping (Google Cloud).** None.
