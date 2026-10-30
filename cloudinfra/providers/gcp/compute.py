"""Compute Engine instances behind the generic compute primitives."""
from ...models.errors import ErrorCode, InfrastructureError
from ...models.request import ComputeRequest
from ...models.resources import ComputeResource, ResourceStatus
from ...observability.logging import event
from ..base import Problem
from . import translate
from .api import COMPUTE
from .identity import email as identity_email

STATUS = {"PROVISIONING": ResourceStatus.CREATING, "STAGING": ResourceStatus.CREATING, "RUNNING": ResourceStatus.READY, "STOPPING": ResourceStatus.UPDATING,
          "SUSPENDING": ResourceStatus.UPDATING, "SUSPENDED": ResourceStatus.STOPPED, "REPAIRING": ResourceStatus.UPDATING, "TERMINATED": ResourceStatus.STOPPED,
          "STOPPED": ResourceStatus.STOPPED}


class GcpCompute:
    def __init__(self, api, settings):
        self.api, self.settings = api, settings

    def _instances(self, zone):
        return f"{COMPUTE}/projects/{self.api.project}/zones/{zone}/instances"

    def _interface(self, spec, network):
        if network is not None:
            interface = {"network": network.provider_id, "subnetwork": network.subnet}
        else:
            interface = {"network": f"projects/{self.api.project}/global/networks/{self.settings.default_network}"}
            if self.settings.default_subnet:
                interface["subnetwork"] = f"projects/{self.api.project}/regions/{spec['region']}/subnetworks/{self.settings.default_subnet}"
        if spec["public_ip"]:
            interface["accessConfigs"] = [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}]
        return interface

    def create(self, spec, planned, ctx, network) -> ComputeResource:
        zone, shape = planned.spec["zone"], planned.spec
        body = {
            "name": planned.name,
            "description": translate.description(ctx),
            "machineType": f"zones/{zone}/machineTypes/{shape['machine_type']}",
            "disks": [{"boot": True, "autoDelete": True, "initializeParams": {"sourceImage": shape["image"], "diskSizeGb": str(shape["boot_disk_gb"]),
                                                                              "diskType": f"zones/{zone}/diskTypes/pd-balanced"}}],
            "networkInterfaces": [self._interface({**shape, "region": ctx.region}, network)],
            "labels": translate.labels(ctx),
            # OS Login: access through IAM, no key material through this service. The machine runs as the request's identity
            # its group names, which holds only the request's grants and has no key; otherwise as no account at all.
            "metadata": {"items": [{"key": "enable-oslogin", "value": "TRUE"}]},
            "serviceAccounts": [{"email": identity_email(self.api.project, ctx.request_id, shape["identity"]), "scopes": ["https://www.googleapis.com/auth/cloud-platform"]}]
            if shape.get("identity") else [],
            # vTPM and integrity monitoring always; Secure Boot as the planned software allows: on, except an image without GPU
            # drivers on a GPU machine, whose drivers Google's installer builds unsigned. A GPU profile with signed drivers keeps it on.
            "shieldedInstanceConfig": {"enableSecureBoot": shape.get("secure_boot", True), "enableVtpm": True, "enableIntegrityMonitoring": True},
        }
        accelerator, scheduling = None, {}
        if shape.get("accelerator"):
            gpu, count = shape["accelerator"]["type"], shape["accelerator"]["count"]
            body["guestAccelerators"] = [{"acceleratorType": f"zones/{zone}/acceleratorTypes/{gpu}", "acceleratorCount": count}]
            scheduling.update({"onHostMaintenance": "TERMINATE", "automaticRestart": True})  # a machine with a GPU cannot live-migrate
            accelerator = f"{gpu} x{count}"
        if shape.get("max_run_hours"):
            # Compute Engine's run-time limit: the timer counts from each start, and a standard machine must name the action.
            scheduling.update({"maxRunDuration": {"seconds": str(int(shape["max_run_hours"]) * 3600)}, "instanceTerminationAction": "STOP"})
        if scheduling:
            body["scheduling"] = scheduling
        resource = ComputeResource(id=planned.spec["resource_id"], name=planned.name, request_id=ctx.request_id, provider="gcp", region=ctx.region, zone=zone,
                                   group=shape.get("group", "vm"), machine_type=shape["machine_type"], cpu=shape.get("cpu", spec.cpu), memory_gb=shape.get("memory_gb", spec.memory_gb),
                                   os_image=shape["image"].rsplit("/", 1)[-1], boot_disk_gb=shape["boot_disk_gb"], accelerator=accelerator, labels=body["labels"],
                                   profile=shape.get("profile"), secure_boot=shape.get("secure_boot", True), max_run_hours=shape.get("max_run_hours"),
                                   autostop_idle_minutes=shape.get("autostop_idle_minutes"), provider_id=f"{self._instances(zone)}/{planned.name}", status=ResourceStatus.CREATING)
        operation = self.api.post(self._instances(zone), json=body)
        ctx.accepted(resource)
        self.api.wait_compute(operation)
        return self.get(resource)

    def stop(self, resource: ComputeResource, ctx) -> ComputeResource:
        self.api.wait_compute(self.api.post(f"{self._instances(resource.zone)}/{resource.name}/stop"))
        return self.get(resource)

    def start(self, resource: ComputeResource, ctx) -> ComputeResource:
        """Start a stopped machine, or resume a suspended one: Compute Engine starts only a stopped instance."""
        base = f"{self._instances(resource.zone)}/{resource.name}"
        action = "resume" if self.api.get(base).get("status") == "SUSPENDED" else "start"
        self.api.wait_compute(self.api.post(f"{base}/{action}"))
        return self.get(resource.model_copy(update={"stop_reason": None}))

    def get(self, resource: ComputeResource) -> ComputeResource:
        try:
            instance = self.api.get(f"{self._instances(resource.zone)}/{resource.name}")
        except InfrastructureError as exc:
            if exc.code == ErrorCode.NOT_FOUND:
                return resource.model_copy(update={"status": ResourceStatus.DELETED, "internal_ip": None, "public_ip": None})
            raise
        return self._from_instance(instance, resource)

    def _from_instance(self, instance: dict, base: ComputeResource) -> ComputeResource:
        interfaces = instance.get("networkInterfaces") or [{}]
        access = interfaces[0].get("accessConfigs") or [{}]
        machine = instance.get("machineType", base.machine_type).rsplit("/", 1)[-1]
        shape = translate.machine_shape(machine) or (base.cpu, base.memory_gb)
        gpus = instance.get("guestAccelerators") or []
        accelerator = f"{gpus[0].get('acceleratorType', '').rsplit('/', 1)[-1]} x{gpus[0].get('acceleratorCount', 1)}" if gpus else base.accelerator
        return base.model_copy(update={
            "status": STATUS.get(instance.get("status", ""), ResourceStatus.UNKNOWN), "machine_type": machine, "cpu": shape[0], "memory_gb": shape[1], "accelerator": accelerator,
            "internal_ip": interfaces[0].get("networkIP"), "public_ip": access[0].get("natIP"), "provider_id": instance.get("selfLink", base.provider_id),
            "labels": instance.get("labels") or base.labels, "created_at": base.created_at or instance.get("creationTimestamp"),
            "instance_id": str(instance["id"]) if instance.get("id") is not None else base.instance_id,
            "started_at": instance.get("lastStartTimestamp") or base.started_at,
        })

    def list(self, labels: dict[str, str]) -> list[ComputeResource]:
        """Instances carrying every given label, through the aggregated list of the project."""
        condition = " AND ".join(f'labels.{key}="{value}"' for key, value in labels.items())
        found, page = [], None
        while True:
            params = {"filter": condition, "pageToken": page} if page else {"filter": condition}
            data = self.api.get(f"{COMPUTE}/projects/{self.api.project}/aggregated/instances", **params)
            for scope in (data.get("items") or {}).values():
                for instance in scope.get("instances") or []:
                    zone = instance.get("zone", "").rsplit("/", 1)[-1]
                    machine = instance.get("machineType", "").rsplit("/", 1)[-1]
                    shape = translate.machine_shape(machine) or (0, 0)
                    base = ComputeResource(id="cmp-000000000000", name=instance["name"], request_id=(instance.get("labels") or {}).get("cloudinfra-request", "unknown"),
                                           provider="gcp", region=zone.rsplit("-", 1)[0], zone=zone, machine_type=machine, cpu=shape[0], memory_gb=shape[1],
                                           os_image="unknown", boot_disk_gb=0, group=(instance.get("labels") or {}).get("cloudinfra-group", "vm"))
                    found.append(self._from_instance(instance, base))
            page = data.get("nextPageToken")
            if not page:
                return found

    def shape(self, resource: ComputeResource, cpu: int, memory_gb: int) -> tuple[str, int, int] | Problem:
        """The machine type a new shape gives a machine, or the problem. A machine with GPUs keeps them, and so their family:
        the shape is chosen as at creation, where a fixed-shape family (L4, A100, H100) gives the smallest shape covering it."""
        gpus = translate.accelerator_of(resource.accelerator)
        if resource.accelerator and gpus is None:
            return Problem(ErrorCode.INVALID_COMPUTE_CONFIGURATION, f"{resource.name} carries GPUs this adapter does not know ({resource.accelerator}); it is not resized",
                           "accelerator")
        return translate.compute_shape(ComputeRequest(cpu=cpu, memory_gb=memory_gb, accelerator=gpus))

    def resize(self, resource: ComputeResource, cpu: int, memory_gb: int, ctx) -> ComputeResource:
        shape = self.shape(resource, cpu, memory_gb)
        if isinstance(shape, Problem):
            raise InfrastructureError(shape.code, shape.message, {"cpu": cpu, "memory_gb": memory_gb, "accelerator": resource.accelerator})
        machine, cpu, memory_gb = shape
        if machine == resource.machine_type:
            return self.get(resource)  # already that shape: nothing to stop or change
        base = f"{self._instances(resource.zone)}/{resource.name}"
        event("provider_resize", provider="gcp", resource=resource.id, machine_type=machine)
        running = self.get(resource).status != ResourceStatus.STOPPED  # a stopped machine is resized and stays stopped
        if running:
            self.api.wait_compute(self.api.post(f"{base}/stop"))
        self.api.wait_compute(self.api.post(f"{base}/setMachineType", json={"machineType": f"zones/{resource.zone}/machineTypes/{machine}"}))
        if running:
            self.api.wait_compute(self.api.post(f"{base}/start"))
        return self.get(resource.model_copy(update={"machine_type": machine, "cpu": cpu, "memory_gb": memory_gb}))

    def delete(self, resource: ComputeResource, ctx) -> None:
        try:
            self.api.wait_compute(self.api.delete(f"{self._instances(resource.zone)}/{resource.name}"))
        except InfrastructureError as exc:
            if exc.code != ErrorCode.NOT_FOUND:
                raise

    def attach_disk(self, compute: ComputeResource, disk_self_link: str, device_name: str) -> None:
        body = {"source": disk_self_link, "deviceName": device_name, "autoDelete": False}
        self.api.wait_compute(self.api.post(f"{self._instances(compute.zone)}/{compute.name}/attachDisk", json=body))
