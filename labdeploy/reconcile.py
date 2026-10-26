"""Offline-controller restore admission: run only after the old writer is fenced and a consistent database is restored."""
import argparse
import json
import time
from pathlib import Path

from . import operations
from .cloud import Observer
from .config import load_config
from .controller import Controller, Store
from .github import GitHub


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("action", choices=["hold", "import-intents", "status", "resume", "release-job"])
    parser.add_argument("--evidence", type=Path, help="Reviewed record required for resume and release-job")
    parser.add_argument("--operation", help="The job admission that release-job frees")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    store = Store(config.database)
    if args.action == "hold":
        store.recovery_mode(True)
    elif args.action == "import-intents":
        store.recovery_mode(True)
        if not config.observer or not config.backup_bucket:
            raise ValueError("Restore requires an observer and the off-host intent bucket")
        controller = Controller(config, GitHub.from_config(config), store, Observer(config))
        witnesses = operations.Intents(config.backup_bucket, controller.observer.token, prefix=config.backup_prefix + "/intents/v1").list()
        print(json.dumps(operations.restore_intents(store, witnesses)))
        return
    elif args.action == "resume":
        if args.evidence is None:
            raise ValueError("Resume requires a reviewed evidence file")
        evidence = json.loads(args.evidence.read_text())
        if (evidence.get("old_writer_fenced") is not True or not evidence.get("operator") or not evidence.get("provider_reconciliation")
                or not evidence.get("quota_reconciliation") or not evidence.get("intent_listing_complete")):
            raise ValueError("Evidence must name the operator, writer fence, complete intent listing, provider and quota reconciliation")
        with store.connect() as db:
            if db.execute("SELECT 1 FROM outbox WHERE state IN ('sending','unknown','prepared') LIMIT 1").fetchone():
                raise ValueError("Unresolved mutation intents remain; admission stays paused")
        store.record_event("restore_resume", identity=evidence["operator"], outcome="reviewed", detail=operations.digest(evidence))
        store.recovery_mode(False)
    elif args.action == "release-job":
        # A launch whose outcome stayed unknown keeps its job's concurrency slot until a person has looked at Cloud Run and
        # found no execution for it. The day's charge stays: a released slot is never a refund.
        if args.evidence is None or not args.operation:
            raise ValueError("release-job requires --operation and a reviewed evidence file")
        evidence = json.loads(args.evidence.read_text())
        if not evidence.get("operator") or evidence.get("operation") != args.operation or evidence.get("no_execution_found") is not True:
            raise ValueError("Evidence must name the operator and this operation, and confirm no execution carries its id")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE job_admissions SET state='not_executed' WHERE id=? AND state IN ('reserved','sending','unknown')",
                                 (args.operation,)).rowcount
            if not changed:
                raise ValueError("No unresolved job admission has that id")
            db.execute("UPDATE outbox SET state='completed', updated=? WHERE operation_id=?", (time.time(), args.operation))
            operations.transition(db, args.operation, "failed")
        store.record_event("job_slot_released", identity=evidence["operator"], outcome="reviewed", detail=operations.digest(evidence))
    print(json.dumps(store.operation_status()))


if __name__ == "__main__":
    main()
