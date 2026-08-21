#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

"""Find devices in the registry that nothing is behind any more.

The find_broken_*.py tools all detect the same shape of problem: configuration
that points at an entity which no longer exists. This one detects the opposite,
which those tools are blind to -- a device the registry still lists although it
produces nothing. The case that motivated it was a duplicate APC SMT1500 left
behind by a second apcupsd config entry: 48 entities on one device, 0 on the
other, and every health check passing.

Two signals are deliberately NOT used, because both were measured against a
real instance and both are wrong:

  * A duplicate device NAME means nothing. Home Assistant's roadmap intent
    (home-assistant/tasks#6) is that a device name does not repeat the area it
    is in, so "Window Candle" legitimately appears once per room. Duplicates are
    matched on `connections`/`identifiers` tuples instead -- the registry's own
    identity keys, which are unique by construction.

  * An entity referenced by nothing means nothing either. 97% of a normal
    registry (1980 of 2038 entities, measured) is referenced by no automation
    and no dashboard, because sensors mostly exist to be looked at. That check
    is available behind --include-unreferenced, and is informational only.

Nothing here mutates Home Assistant. The remedy for an orphan is usually to
delete its config entry, which is a far larger hammer than the reference
rewrites the other tools perform, so the config entry ID is printed and removal
is left as a deliberate manual step.
"""

import argparse
from collections import defaultdict

import argcomplete
import tabulate

import common

tabulate.PRESERVE_WHITESPACE = True

# Relation types search/related reports for EVERY device, orphaned or not.
# Anything outside this set means something genuinely points at the device.
AMBIENT_RELATIONS = {"area", "floor", "config_entry", "integration", "label"}

# Integrations whose devices are expected to carry no entities of their own.
# A Bluetooth proxy registers one device per discovered MAC; having no entities
# is correct and permanent, not a leak.
ENTITYLESS_INTEGRATIONS = {"bluetooth"}


def device_name(device):
    return device.get("name_by_user") or device.get("name") or "(unnamed)"


def identity_keys(device):
    """The registry's own uniqueness keys for a device, as hashable tuples."""
    keys = []
    for pair in device.get("connections") or []:
        keys.append(("connection", tuple(pair)))
    for pair in device.get("identifiers") or []:
        keys.append(("identifier", tuple(pair)))
    return keys


def find_orphaned_devices(ws, verbose=False, include_unreferenced=False):
    msg_id = 1

    print("Fetching device and entity registries...")
    devices, msg_id = common.get_device_registry(ws, msg_id)
    entities, msg_id = common.get_entity_registry(ws, msg_id)
    print(f"Found {len(devices)} devices and {len(entities)} entities.")

    entities_by_device = defaultdict(list)
    for entry in entities:
        if entry.get("device_id"):
            entities_by_device[entry["device_id"]].append(entry["entity_id"])

    orphans = []
    stale = []
    suppressed = []

    for device_id, device in devices.items():
        if entities_by_device.get(device_id):
            continue

        name = device_name(device)
        config_entries = device.get("config_entries") or []

        # A device whose last config entry went away is a different failure from
        # one whose integration is still loaded but produces nothing.
        if not config_entries:
            stale.append((name, device_id, device.get("area_id") or "-"))
            continue

        if device.get("disabled_by"):
            suppressed.append((name, f"disabled by {device['disabled_by']}"))
            continue

        if device.get("entry_type") == "service":
            suppressed.append((name, "entry_type=service (not a physical device)"))
            continue

        parent_id = device.get("via_device_id")
        if parent_id and parent_id in devices:
            suppressed.append(
                (name, f"sub-device of {device_name(devices[parent_id])}")
            )
            continue

        related, msg_id = common.find_related(ws, "device", device_id, msg_id)
        integrations = set(related.get("integration") or [])
        if integrations & ENTITYLESS_INTEGRATIONS:
            suppressed.append(
                (name, f"entity-less integration: {', '.join(sorted(integrations))}")
            )
            continue

        referrers = sorted(set(related) - AMBIENT_RELATIONS)
        if referrers:
            suppressed.append((name, f"still referenced by: {', '.join(referrers)}"))
            continue

        orphans.append(
            (
                name,
                ", ".join(sorted(integrations)) or "-",
                ", ".join(config_entries),
                device_id,
            )
        )

    duplicates = []
    by_identity = defaultdict(list)
    for device_id, device in devices.items():
        for key in identity_keys(device):
            by_identity[key].append(device_id)
    seen_pairs = set()
    for key, device_ids in by_identity.items():
        if len(device_ids) < 2:
            continue
        pair = tuple(sorted(device_ids))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        kind, value = key
        duplicates.append(
            (
                f"{kind}: {':'.join(str(v) for v in value)}",
                " / ".join(device_name(devices[d]) for d in device_ids),
                " / ".join(str(len(entities_by_device.get(d, []))) for d in device_ids),
            )
        )

    found = False

    if orphans:
        found = True
        print("\nOrphaned Devices (no entities, nothing referring to them):")
        print(
            tabulate.tabulate(
                orphans,
                headers=["Device", "Integration", "Config Entry", "Device ID"],
                tablefmt="github",
            )
        )
        print(
            "\nTo remove one, delete its config entry -- Settings > Devices &"
            "\nServices, or DELETE /api/config/config_entries/entry/<id>."
            "\nRemoving the device alone leaves the entry to recreate it."
        )

    if stale:
        found = True
        print("\nStale Devices (no entities and no config entry at all):")
        print(
            tabulate.tabulate(
                stale, headers=["Device", "Device ID", "Area"], tablefmt="github"
            )
        )

    if duplicates:
        found = True
        print("\nDuplicate Devices (two registry entries sharing one identity):")
        print(
            tabulate.tabulate(
                duplicates,
                headers=["Shared identity", "Devices", "Entity counts"],
                tablefmt="github",
            )
        )

    if include_unreferenced:
        referenced, msg_id = _referenced_entities(ws, msg_id, verbose)
        all_ids = {e["entity_id"] for e in entities}
        unreferenced = sorted(all_ids - referenced)
        by_domain = defaultdict(int)
        for entity_id in unreferenced:
            by_domain[entity_id.split(".", 1)[0]] += 1
        print(
            f"\nUnreferenced entities (informational): "
            f"{len(unreferenced)} of {len(all_ids)}"
        )
        print(
            tabulate.tabulate(
                sorted(by_domain.items(), key=lambda kv: -kv[1]),
                headers=["Domain", "Count"],
                tablefmt="github",
            )
        )
        if verbose:
            for entity_id in unreferenced:
                print(f"  {entity_id}")

    if verbose and suppressed:
        print("\nSuppressed (expected to have no entities):")
        print(
            tabulate.tabulate(
                suppressed, headers=["Device", "Reason"], tablefmt="github"
            )
        )

    if not found:
        print("\nNo orphaned devices found.")

    return found


def _referenced_entities(ws, msg_id, verbose):
    """Every entity ID named by an automation, script or dashboard."""
    import json
    import re

    referenced = set()
    pattern = re.compile(common.ENTITY_ID_IN_QUOTES_PATTERN, re.IGNORECASE)

    entities, msg_id = common.get_valid_entities(ws, msg_id)

    def scan(config_data):
        if config_data:
            referenced.update(pattern.findall(json.dumps(config_data)))

    for entity_id in sorted(entities):
        if entity_id.startswith("automation."):
            config_data, msg_id = common.get_automation_config(ws, entity_id, msg_id)
            scan(config_data)
        elif entity_id.startswith("script."):
            config_data, msg_id = common.get_script_config(ws, entity_id, msg_id)
            scan(config_data)
        elif entity_id.startswith("scene."):
            config_data, msg_id = common.get_scene_config(ws, entity_id, msg_id)
            scan(config_data)

    dashboards, msg_id = common.list_dashboards(ws, msg_id)
    for dashboard in dashboards or []:
        config_data, msg_id = common.get_dashboard_config(
            ws, dashboard.get("url_path"), msg_id
        )
        scan(config_data)

    if verbose:
        print(f"Collected {len(referenced)} referenced entity IDs.")
    return referenced, msg_id


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find Orphaned Devices")
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show detailed progress"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help=(
            "Accepted for symmetry with the other checks, but this one never "
            "writes: the remedy is deleting a config entry, which is too "
            "destructive to automate."
        ),
    )
    parser.add_argument(
        "--include-unreferenced",
        action="store_true",
        help="Also summarise entities that no automation or dashboard names",
    )
    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    if args.fix:
        print(
            "Note: --fix does nothing here. Removing an orphaned device means "
            "deleting its config entry; the ID is printed below so you can do "
            "that deliberately.\n"
        )

    with common.websocket_context() as ws:
        if ws:
            if find_orphaned_devices(ws, args.verbose, args.include_unreferenced):
                import sys

                sys.exit(1)
