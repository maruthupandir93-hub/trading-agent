"""Grab an Oracle Cloud Ampere A1.Flex the moment capacity frees up, and tell you.

THE PROBLEM
===========
Oracle's Always-Free Ampere (VM.Standard.A1.Flex) is chronically out of capacity in
popular regions:

    Out of capacity for shape VM.Standard.A1.Flex in availability domain AD-1.
    Create the instance in a different availability domain or try again later.

There is NO API that reports "capacity is available". The only way to know is to
attempt the launch and read the result: a `500 InternalError - Out of host
capacity` means keep trying; a success means you got the machine. So this monitor
does exactly what a human does by hand, on a timer and across every availability
domain — and the instant a launch succeeds it messages your Telegram real channel
with the instance's details.

WHAT IT ACTUALLY DOES, STATED PLAINLY
=====================================
It CREATES the instance when capacity appears. "Notify when available" and "create
when available" are the same action here, because the successful create IS the
detection. On the first success it STOPS (it will never create a second), and it
first checks whether an instance of the same name already exists so a restart does
not make a duplicate.

It is a TEMPORARY utility, separate from the trading app. It only borrows the
Telegram bot token and the REAL channel id from `.env`.

SETUP (one time)
================
1. Install the SDK into the project venv:
       .venv/Scripts/python.exe -m pip install oci
2. Configure OCI auth. Easiest is an API key:
       - In the OCI console: Profile -> User settings -> API keys -> Add API key,
         download the private key, and paste the shown config into ~/.oci/config
         (or run `oci setup config` if you install the CLI).
   OR, if you run this ON an existing OCI VM, set  "auth": "instance_principal"
   in the config file below (the VM's own identity, no key file needed — the VM
   must be in a dynamic group with a policy allowing instance launches).
3. Copy scripts/oracle_vm_config.example.json to scripts/oracle_vm_config.json
   and fill in compartment_id, subnet_id and ssh_authorized_key. The rest is
   auto-discovered (availability domains and the image).
4. Make sure TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID_REAL are set in .env.

RUN
===
    .venv/Scripts/python.exe scripts/oracle_capacity_monitor.py
    .venv/Scripts/python.exe scripts/oracle_capacity_monitor.py --once   (one pass)

Leave it running (or schedule it). Default is one attempt every 10 minutes. On a
laptop that sleeps, prefer running it on a machine that stays up — your existing
backend server is ideal.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The status lines carry emoji; a Windows console defaults to cp1252 and a bare
# `print` of one raises UnicodeEncodeError, which would kill the loop that is doing
# the actual work. Force UTF-8 on stdout, tolerating consoles that cannot render it.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass

import os

CONFIG_PATH = ROOT / "scripts" / "oracle_vm_config.json"
DEFAULT_INTERVAL_S = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Telegram — the REAL channel only, reusing the app's bot
# ---------------------------------------------------------------------------


def notify(text: str) -> None:
    """Send to the Telegram real channel. Never raises — a failed alert must not
    stop the monitor, which is the thing actually doing the work."""
    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.getenv("TELEGRAM_CHAT_ID_REAL") or "").strip()
    if not token or not chat:
        print(f"[telegram OFF] {text}")
        return
    try:
        import httpx

        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=15,
        )
        if r.status_code >= 400:
            print(f"[telegram HTTP {r.status_code}] {r.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        print(f"[telegram failed] {exc}")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        print(
            f"Config not found: {CONFIG_PATH}\n"
            f"Copy scripts/oracle_vm_config.example.json to that path and fill it in."
        )
        sys.exit(2)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if not cfg.get("compartment_id"):
        print("config.compartment_id is required (the OCID of the compartment to create in).")
        sys.exit(2)
    return cfg


# ---------------------------------------------------------------------------
# OCI clients
# ---------------------------------------------------------------------------


def build_clients(cfg: Dict[str, Any]):
    try:
        import oci  # noqa: F401
    except ImportError:
        print("The OCI SDK is not installed. Run:\n"
              "    .venv/Scripts/python.exe -m pip install oci")
        sys.exit(2)

    import oci

    auth = (cfg.get("auth") or "config").lower()
    if auth == "instance_principal":
        signer = oci.auth.signers.InstancePrincipalsSecurityTokenSigner()
        identity = oci.identity.IdentityClient(config={}, signer=signer)
        compute = oci.core.ComputeClient(config={}, signer=signer)
        network = oci.core.VirtualNetworkClient(config={}, signer=signer)
        tenancy_id = signer.tenancy_id
    else:
        oci_cfg = oci.config.from_file(profile_name=cfg.get("oci_profile", "DEFAULT"))
        oci.config.validate_config(oci_cfg)
        identity = oci.identity.IdentityClient(oci_cfg)
        compute = oci.core.ComputeClient(oci_cfg)
        network = oci.core.VirtualNetworkClient(oci_cfg)
        tenancy_id = oci_cfg["tenancy"]
    return identity, compute, network, tenancy_id


def list_availability_domains(identity, tenancy_id: str, cfg: Dict[str, Any]) -> List[str]:
    if cfg.get("availability_domains"):
        return list(cfg["availability_domains"])
    ads = identity.list_availability_domains(tenancy_id).data
    return [ad.name for ad in ads]


def resolve_image(compute, cfg: Dict[str, Any]) -> str:
    """The image to boot. Explicit `image_id` wins; otherwise pick the newest
    aarch64 image matching the requested OS that supports the A1 shape."""
    if cfg.get("image_id"):
        return cfg["image_id"]
    os_name = cfg.get("operating_system", "Canonical Ubuntu")
    os_version = cfg.get("operating_system_version", "22.04")
    images = compute.list_images(
        compartment_id=cfg["compartment_id"],
        operating_system=os_name,
        operating_system_version=os_version,
        shape="VM.Standard.A1.Flex",
        sort_by="TIMECREATED",
        sort_order="DESC",
    ).data
    # Skip aarch64 GPU/minimal variants the name flags, take the newest plain one.
    for img in images:
        if "aarch64" in (img.display_name or "").lower() or "a1" in (img.display_name or "").lower():
            return img.id
    if images:
        return images[0].id
    raise RuntimeError(
        f"No {os_name} {os_version} image found that supports VM.Standard.A1.Flex. "
        f"Set image_id in the config explicitly."
    )


def resolve_subnet(network, cfg: Dict[str, Any]) -> str:
    if cfg.get("subnet_id"):
        return cfg["subnet_id"]
    subnets = network.list_subnets(compartment_id=cfg["compartment_id"]).data
    if not subnets:
        raise RuntimeError(
            "No subnet found in the compartment and none configured. Create a VCN "
            "with a public subnet, or set subnet_id in the config."
        )
    print(f"  using auto-discovered subnet: {subnets[0].display_name} ({subnets[0].id})")
    return subnets[0].id


def already_exists(compute, cfg: Dict[str, Any]) -> Optional[str]:
    """An instance of the target name that is live — so a restart of this monitor
    does not create a second machine. Returns its OCID, or None."""
    name = cfg.get("display_name", "ampere-vm")
    insts = compute.list_instances(compartment_id=cfg["compartment_id"]).data
    for inst in insts:
        if inst.display_name == name and inst.lifecycle_state in (
            "PROVISIONING", "STARTING", "RUNNING",
        ):
            return inst.id
    return None


# ---------------------------------------------------------------------------
# The launch attempt
# ---------------------------------------------------------------------------


def is_capacity_error(exc) -> bool:
    """The retryable 'no capacity right now' case, distinct from a real fault."""
    import oci

    if not isinstance(exc, oci.exceptions.ServiceError):
        return False
    msg = (exc.message or "").lower()
    # 500 InternalError "Out of host capacity" is the usual shape; some regions
    # return 429. Match on the phrase so an unrelated 500 is not mistaken for it.
    return ("capacity" in msg) or (exc.status == 429)


def try_launch(compute, cfg: Dict[str, Any], ad: str, image_id: str, subnet_id: str):
    """One LaunchInstance attempt in one AD. Returns the launched instance, or
    raises the ServiceError (capacity or otherwise) for the caller to classify."""
    import oci

    details = oci.core.models.LaunchInstanceDetails(
        compartment_id=cfg["compartment_id"],
        availability_domain=ad,
        display_name=cfg.get("display_name", "ampere-vm"),
        shape="VM.Standard.A1.Flex",
        shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
            ocpus=float(cfg.get("ocpus", 2)),
            memory_in_gbs=float(cfg.get("memory_gb", 12)),
        ),
        source_details=oci.core.models.InstanceSourceViaImageDetails(
            image_id=image_id,
            boot_volume_size_in_gbs=int(cfg.get("boot_volume_gb", 50)),
        ),
        create_vnic_details=oci.core.models.CreateVnicDetails(
            subnet_id=subnet_id,
            assign_public_ip=bool(cfg.get("assign_public_ip", True)),
        ),
        # No fault_domain on purpose — the capacity error message itself says to
        # let OCI place it. metadata carries the SSH key so you can log in.
        metadata=({"ssh_authorized_keys": cfg["ssh_authorized_key"]}
                  if cfg.get("ssh_authorized_key") else {}),
    )
    return compute.launch_instance(details).data


def public_ip_of(compute, network, cfg: Dict[str, Any], instance_id: str) -> Optional[str]:
    """Best-effort public IP, once the VNIC attaches. None if not ready yet."""
    try:
        attachments = compute.list_vnic_attachments(
            compartment_id=cfg["compartment_id"], instance_id=instance_id
        ).data
        for att in attachments:
            if att.vnic_id:
                vnic = network.get_vnic(att.vnic_id).data
                if vnic.public_ip:
                    return vnic.public_ip
    except Exception:  # noqa: BLE001
        pass
    return None


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


def run(once: bool) -> int:
    cfg = load_config()
    interval = int(cfg.get("interval_seconds", DEFAULT_INTERVAL_S))
    name = cfg.get("display_name", "ampere-vm")

    print(f"Oracle A1.Flex capacity monitor — {cfg.get('ocpus', 2)} OCPU / "
          f"{cfg.get('memory_gb', 12)} GB, every {interval}s")

    identity, compute, network, tenancy_id = build_clients(cfg)

    # Resolve the pieces once — they do not change between attempts, and a config
    # error (bad compartment, no subnet) should fail loudly now, not silently
    # every cycle.
    try:
        image_id = resolve_image(compute, cfg)
        subnet_id = resolve_subnet(network, cfg)
        ads = list_availability_domains(identity, tenancy_id, cfg)
    except Exception as exc:  # noqa: BLE001
        msg = f"Oracle monitor could NOT start: {exc}"
        print(msg)
        notify(f"⚠️ <b>Oracle A1 monitor failed to start</b>\n{exc}")
        return 2

    existing = already_exists(compute, cfg)
    if existing:
        ip = public_ip_of(compute, network, cfg, existing) or "pending"
        msg = (f"✅ <b>{name} already exists</b> ({existing.split('.')[-1][:12]}…). "
               f"IP {ip}. Monitor not needed — stopping.")
        print(msg)
        notify(msg)
        return 0

    print(f"  image={image_id.split('.')[-1][:12]}…  subnet ok  ADs={ads}")
    notify(
        f"🛰️ <b>Oracle A1 capacity monitor started</b>\n"
        f"Watching {len(ads)} availability domain(s) for "
        f"{cfg.get('ocpus', 2)} OCPU / {cfg.get('memory_gb', 12)} GB every "
        f"{interval // 60} min. You'll get a message the moment it launches."
    )

    attempt = 0
    while True:
        attempt += 1
        for ad in ads:
            try:
                inst = try_launch(compute, cfg, ad, image_id, subnet_id)
            except Exception as exc:  # noqa: BLE001
                if is_capacity_error(exc):
                    print(f"  [{time.strftime('%H:%M:%S')}] attempt {attempt} {ad}: out of capacity")
                    continue
                # A NON-capacity error will not fix itself by waiting: bad auth, a
                # service limit already reached (you have your max free A1s), a
                # config mistake. Say so and stop rather than hammer it.
                import oci

                detail = getattr(exc, "message", str(exc))
                code = getattr(exc, "code", type(exc).__name__)
                msg = (f"⛔ <b>Oracle A1 monitor stopped</b> — this error will not "
                       f"resolve by retrying:\n<b>{code}</b>: {detail}")
                print(msg)
                notify(msg)
                return 1

            # SUCCESS. Stop immediately so a second instance is never created.
            iid = inst.id
            print(f"  LAUNCHED in {ad}: {iid}")
            # Give the VNIC a moment to attach so the alert can carry an IP.
            ip = None
            for _ in range(6):
                ip = public_ip_of(compute, network, cfg, iid)
                if ip:
                    break
                time.sleep(5)
            notify(
                f"🎉 <b>GOT THE ORACLE A1!</b>\n"
                f"<b>{name}</b> launched in {ad}\n"
                f"Public IP: <b>{ip or 'provisioning — check the console'}</b>\n"
                f"OCID: <code>{iid}</code>\n"
                f"{cfg.get('ocpus', 2)} OCPU / {cfg.get('memory_gb', 12)} GB. "
                f"SSH in once it reaches RUNNING."
            )
            print("Done. Instance created; monitor exiting.")
            return 0

        if once:
            print("  --once: one pass done, no capacity this time.")
            return 3

        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="one attempt across all ADs, then exit")
    args = ap.parse_args()
    try:
        return run(args.once)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
