#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

import argparse
import json
import re
import tabulate
import common
import argcomplete

tabulate.PRESERVE_WHITESPACE = True


def list_entities(ws, search_regex=None):
    msg_id = 1
    ws.send(json.dumps({"id": msg_id, "type": "config/entity_registry/list"}))
    result = ws.recv()
    result = json.loads(result)

    if not result["success"]:
        print("Failed to list entities.")
        return []

    entities = result["result"]

    # Filter out entities that don't belong to a device (e.g. helper groups)
    entities = [e for e in entities if e.get("device_id")]

    if search_regex:
        entities = [e for e in entities if re.search(search_regex, e["entity_id"])]

    if not entities:
        print(
            "No entities found"
            + (f" matching '{search_regex}'" if search_regex else "")
            + "."
        )
        return []

    return entities


def update_automation_references(ws, updates, msg_id, execute=False, verbose=False):
    print("\nChecking for automation references to update...")

    automation_updates = {}

    for old_id, new_id in updates:
        automations, msg_id = common.find_related_automations(ws, old_id, msg_id)
        for auto_id in automations:
            if auto_id not in automation_updates:
                automation_updates[auto_id] = []
            automation_updates[auto_id].append((old_id, new_id))

    if not automation_updates:
        print("No related automations found.")
        return msg_id

    print(f"Found {len(automation_updates)} automations to update.")

    for auto_entity_id, replacements in automation_updates.items():
        config_data, msg_id = common.get_automation_config(ws, auto_entity_id, msg_id)
        if not config_data:
            print(f"Could not fetch config for {auto_entity_id}")
            continue

        # Use common.replace_references for safe replacement
        modified = False
        for old_id, new_id in replacements:
            if common.replace_references(config_data, old_id, new_id):
                modified = True
                if execute:
                    print(
                        f"  Updating reference {old_id} -> {new_id} in {auto_entity_id}"
                    )

        if modified:
            if execute:
                if common.save_automation_config(config_data):
                    print(f"  Successfully saved automation {auto_entity_id}")
                else:
                    print(f"  Failed to save automation {auto_entity_id}")
            else:
                print(f"  [Dry Run] Would update references in {auto_entity_id}")
                for old_id, new_id in replacements:
                    print(f"    - {old_id} -> {new_id}")
        else:
            if verbose:
                print(
                    f"  No actual references found in {auto_entity_id} (might be indirect)"
                )

    return msg_id


def process_entities(ws, entities, execute=False, recreate_ids=True, verbose=False):
    if not entities:
        return

    msg_id = 100  # Start safely above list_entities id

    # Fetch device registry to handle custom device names
    devices, msg_id = common.get_device_registry(ws, msg_id)

    # Check for automatic entity ID updates (First Pass)
    updates = []
    if recreate_ids:
        updates, msg_id = get_automatic_updates(
            ws, [e["entity_id"] for e in entities], msg_id
        )

    # Apply automatic entity ID updates (First Pass)
    if recreate_ids and updates:
        if execute:
            msg_id = apply_automatic_updates(ws, updates, msg_id)
            update_local_entity_ids(entities, updates)

        # Update automation references (First Pass)
        msg_id = update_automation_references(
            ws, updates, msg_id, execute=execute, verbose=verbose
        )

    # Prepare data for table
    # Columns: Entity ID, Current Name, Proposed Name
    table_data = []
    for e in entities:
        current_name = e.get("name")

        # Determine proposed name
        target_name = None  # Default target is None (reset to default)

        device_id = e.get("device_id")
        if device_id and device_id in devices:
            device = devices[device_id]
            user_device_name = device.get("name_by_user")
            original_name = e.get("original_name")

            # If the device has a user-defined name, and the entity has an original name
            if user_device_name and original_name:
                # Check if the original name starts with the device's *default* name
                default_device_name = device.get("name")
                device_model = device.get("model")

                if default_device_name and original_name.startswith(
                    default_device_name
                ):
                    suffix = original_name[len(default_device_name) :].strip()
                    target_name = f"{user_device_name} {suffix}".strip()
                elif device_model and original_name.startswith(device_model):
                    suffix = original_name[len(device_model) :].strip()
                    target_name = f"{user_device_name} {suffix}".strip()

        # Compare target_name with current_name
        proposed_name = None
        if current_name != target_name:
            if target_name is None:
                proposed_name = "None"
            else:
                proposed_name = target_name

        if proposed_name:
            table_data.append((e["entity_id"], str(current_name), proposed_name))
        elif verbose:
            table_data.append((e["entity_id"], str(current_name), "No Change"))

    # Print table
    if table_data:
        print("")
        headers = ["Entity ID", "Current Name", "Proposed Name"]
        table = [headers] + common.align_strings(table_data)
        print(tabulate.tabulate(table, headers="firstrow", tablefmt="github"))
    elif not verbose:
        print(
            "\nNo entities found with custom names (or needing updates). Use --verbose to see all matched entities."
        )

    if not execute:
        print("\nDry run complete. Use --execute or -y to apply changes.")
        return

    # Apply name changes
    msg_id = apply_name_changes(ws, table_data, verbose, msg_id)

    # Check for automatic entity ID updates (Second Pass)
    if recreate_ids:
        updates, msg_id = get_automatic_updates(
            ws, [e["entity_id"] for e in entities], msg_id
        )

        # Apply automatic entity ID updates (Second Pass)
        if updates:
            msg_id = apply_automatic_updates(ws, updates, msg_id)
            update_local_entity_ids(entities, updates)

            # Update automation references (Second Pass)
            msg_id = update_automation_references(
                ws, updates, msg_id, execute=True, verbose=verbose
            )


def apply_name_changes(ws, table_data, verbose, msg_id):
    print("\nApplying name changes...")
    for row in table_data:
        entity_id = row[0]
        proposed_name = row[2]

        if proposed_name == "No Change":
            continue

        # Convert "None" string back to actual None
        new_name = None if proposed_name == "None" else proposed_name

        msg_id += 1
        update_msg = {
            "id": msg_id,
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
            "name": new_name,
        }
        ws.send(json.dumps(update_msg))
        update_result = ws.recv()
        update_result = json.loads(update_result)

        if update_result["success"]:
            print(f"Successfully updated {entity_id} to '{new_name}'")
        else:
            error_msg = update_result.get("error", {}).get("message", "Unknown error")
            print(f"Failed to update {entity_id}: {error_msg}")

    return msg_id


def get_automatic_updates(ws, entity_ids, msg_id):
    print("\nChecking for automatic entity ID updates...")
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "config/entity_registry/get_automatic_entity_ids",
                "entity_ids": entity_ids,
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)

    updates = []
    if result["success"]:
        res_map = result["result"]
        for entity_id, new_entity_id in res_map.items():
            if new_entity_id and new_entity_id != entity_id:
                updates.append((entity_id, new_entity_id))

        if updates:
            print("\nThe following Entity IDs will be updated:")
            print(
                tabulate.tabulate(
                    updates,
                    headers=["Current Entity ID", "New Entity ID"],
                    tablefmt="github",
                )
            )
        else:
            print("No automatic entity ID updates found.")
    else:
        print("Failed to get automatic entity IDs.")

    return updates, msg_id


def apply_automatic_updates(ws, updates, msg_id):
    if not updates:
        return msg_id

    print("\nApplying automatic entity ID updates...")
    for entity_id, new_entity_id in updates:
        msg_id += 1
        update_msg = {
            "id": msg_id,
            "type": "config/entity_registry/update",
            "entity_id": entity_id,
            "new_entity_id": new_entity_id,
        }
        ws.send(json.dumps(update_msg))
        result = ws.recv()
        result = json.loads(result)

        if result["success"]:
            print(f"Successfully renamed {entity_id} to {new_entity_id}")
        else:
            error_msg = result.get("error", {}).get("message", "Unknown error")
            print(f"Failed to rename {entity_id}: {error_msg}")

    return msg_id


def update_local_entity_ids(target_entities, updates):
    update_map = {old: new for old, new in updates}
    for e in target_entities:
        if e["entity_id"] in update_map:
            e["entity_id"] = update_map[e["entity_id"]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Reset Entity Names to Null")
    parser.add_argument(
        "--execute", "-y", action="store_true", help="Execute the renaming process"
    )
    parser.add_argument(
        "--search",
        dest="search_regex",
        help="Regular expression for search. Note: Only searches entity IDs.",
    )
    parser.add_argument(
        "--no-recreate-ids",
        dest="recreate_ids",
        action="store_false",
        help="Disable automatic entity ID recreation",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Show all entities, including those with empty names",
    )
    parser.set_defaults(recreate_ids=True)
    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    with common.websocket_context() as ws:
        if ws:
            entities = list_entities(ws, args.search_regex)
            process_entities(
                ws, entities, args.execute, args.recreate_ids, args.verbose
            )
