"""
Simulated SU inbox — the Part 2 trigger.

The missing piece between Part 1 and the real CG workflow was never the model:
it was that the pipeline only ran when someone uploaded a file to a screen.
This module makes the agent wake up when SU's email arrives.

The email plumbing is mocked as a watched folder (the brief's suggestion): one
"email" is a directory dropped into `inbox/` containing

    email.json          {"from": ..., "subject": ..., "customer": ..., "body": ...}
    <attachments>       any mix of .pdf / .png / .jpg / .jpeg / .webp

Convention: attachments are written first and `email.json` last, so a folder is
picked up only when it is complete. A background thread polls every couple of
seconds; each new email becomes one shipment + one run per attachment, the
folder is moved to `inbox/processed/`, and the shipment pipeline starts in its
own thread. Swapping this for real plumbing (IMAP poll, webhook) replaces only
this file — everything downstream is identical.

Fail-loud policy: an email with an unknown customer or no readable attachment
still becomes a shipment row — marked FAILED with the reason — so it shows up
in the CG queue instead of silently rotting in a folder.
"""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import uuid

from app import orchestrator
from app.agents.validator import available_customers
from app.db import store
from app.models import ShipmentStatus
from app.settings import settings

ATTACHMENT_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
PROCESSED_SUBDIR = "processed"


def _dirs() -> tuple[str, str]:
    inbox = settings.inbox_dir
    processed = os.path.join(inbox, PROCESSED_SUBDIR)
    os.makedirs(processed, exist_ok=True)
    return inbox, processed


def process_email_dir(path: str) -> str:
    """Turn one complete email folder into a shipment + runs. Returns shipment_id.

    The folder is moved to processed/ BEFORE the pipeline starts, so a crash
    mid-pipeline cannot double-ingest the same email on restart (the shipment
    row itself is the recovery checkpoint from then on).
    """
    _, processed = _dirs()
    meta = {}
    try:
        with open(os.path.join(path, "email.json")) as f:
            meta = json.load(f)
    except Exception:  # noqa: BLE001 — malformed manifest still surfaces as a failed shipment
        pass

    shipment_id = str(uuid.uuid4())
    customer = str(meta.get("customer") or "UNKNOWN")
    store.create_shipment(
        shipment_id, customer,
        from_addr=str(meta.get("from") or "unknown@supplier"),
        subject=str(meta.get("subject") or os.path.basename(path)),
    )

    attachments = sorted(
        f for f in os.listdir(path)
        if os.path.splitext(f)[1].lower() in ATTACHMENT_EXTS
    )

    if customer not in available_customers():
        store.update_shipment(
            shipment_id, ShipmentStatus.FAILED,
            error=f"Unknown customer '{customer}' — no rule set configured. "
                  f"Configured: {available_customers()}. Never falling back to another customer's rules.")
    elif not attachments:
        store.update_shipment(shipment_id, ShipmentStatus.FAILED,
                              error="Email contained no readable attachments (pdf/png/jpg/webp).")
    else:
        os.makedirs(settings.upload_dir, exist_ok=True)
        for name in attachments:
            run_id = str(uuid.uuid4())
            ext = os.path.splitext(name)[1].lower()
            source_path = os.path.join(settings.upload_dir, f"{run_id}{ext}")
            shutil.copyfile(os.path.join(path, name), source_path)
            store.create_run(run_id, customer, name, source_path=source_path,
                             shipment_id=shipment_id)

    shutil.move(path, os.path.join(processed, f"{shipment_id}-{os.path.basename(path)}"))
    return shipment_id


# Folders that blew up mid-processing. Never rescanned: the shipment row may
# already exist, so re-processing would mint a duplicate every poll tick.
_quarantined: set = set()


def scan_once() -> list[str]:
    """Pick up every complete email folder currently in the inbox. Each ready
    shipment's pipeline is started on its own thread so scanning never blocks."""
    inbox, processed = _dirs()
    started: list[str] = []
    for name in sorted(os.listdir(inbox)):
        path = os.path.join(inbox, name)
        if name == PROCESSED_SUBDIR or name.startswith(".") or not os.path.isdir(path):
            continue
        if path in _quarantined:
            continue
        if not os.path.exists(os.path.join(path, "email.json")):
            continue  # still being written — email.json arrives last by convention
        try:
            shipment_id = process_email_dir(path)
        except Exception:  # noqa: BLE001 — one bad email must not wedge the watcher
            _quarantined.add(path)
            try:
                shutil.move(path, os.path.join(processed, f"quarantined-{name}"))
            except Exception:  # noqa: BLE001 — unmovable folder stays in _quarantined
                pass
            continue
        rec = store.get_shipment(shipment_id)
        if rec and rec.status != ShipmentStatus.FAILED:
            threading.Thread(target=orchestrator.run_shipment, args=(shipment_id,),
                             daemon=True).start()
        started.append(shipment_id)
    return started


def start_watcher() -> None:
    """Poll the inbox forever (daemon thread — dies with the process)."""

    def _loop() -> None:
        while True:
            try:
                scan_once()
            except Exception:  # noqa: BLE001 — the watcher must survive any bad email
                pass
            time.sleep(settings.inbox_poll_seconds)

    threading.Thread(target=_loop, daemon=True).start()
