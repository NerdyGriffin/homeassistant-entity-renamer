#!/usr/bin/env python3

import argparse
import config
import json
import re
import requests
import ssl
import tabulate
import websocket

tabulate.PRESERVE_WHITESPACE = True

# Determine the protocol based on TLS configuration
TLS_S = "s" if config.TLS else ""

# Header containing the access token
headers = {
    "Authorization": f"Bearer {config.ACCESS_TOKEN}",
    "Content-Type": "application/json",
}


def align_strings(table):
    alignment_char = "."

    if len(table) == 0:
        return

    for column in range(len(table[0])):
        # Get the column data from the table
        column_data = [row[column] for row in table]

        # Find the maximum length of the first part of the split strings
        strings_to_align = [s for s in column_data if alignment_char in s]
        if len(strings_to_align) == 0:
            continue

        max_length = max([len(s.split(alignment_char)[0]) for s in strings_to_align])

        def align_string(s):
            s_split = s.split(alignment_char, maxsplit=1)
            if len(s_split) == 1:
                return s
            else:
                return f"{s_split[0]:>{max_length}}.{s_split[1]}"

        # Create the modified table by replacing the column with aligned strings
        table = [
            tuple(
                align_string(value) if i == column else value
                for i, value in enumerate(row)
            )
            for row in table
        ]

    return table


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


def find_related_automations(ws, entity_id, msg_id):
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "search/related",
                "item_type": "entity",
                "item_id": entity_id,
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)

    automations = []
    if result["success"]:
        if "automation" in result["result"]:
            automations = result["result"]["automation"]

    return automations, msg_id


def get_automation_config(ws, automation_entity_id, msg_id):
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "automation/config",
                "entity_id": automation_entity_id,
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        return result["result"], msg_id
    return None, msg_id


def save_automation_config(automation_config):
    automation_id = automation_config.get("id")
    if not automation_id:
        print("Error: Automation config missing ID.")
        return False

    url = f"http{TLS_S}://{config.HOST}/api/config/automation/config/{automation_id}"
    headers = {
        "Authorization": f"Bearer {config.ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        url, headers=headers, json=automation_config, verify=config.SSL_VERIFY
    )
    if response.status_code == 200:
        return True
    else:
        print(f"Failed to save automation {automation_id}: {response.text}")
        return False


def update_automation_references(ws, updates, msg_id, execute=False):
    print("\nChecking for automation references to update...")

    automation_updates = {}

    for old_id, new_id in updates:
        automations, msg_id = find_related_automations(ws, old_id, msg_id)
        for auto_id in automations:
            if auto_id not in automation_updates:
                automation_updates[auto_id] = []
            automation_updates[auto_id].append((old_id, new_id))

    if not automation_updates:
        print("No related automations found.")
        return msg_id

    print(f"Found {len(automation_updates)} automations to update.")

    for auto_entity_id, replacements in automation_updates.items():
        config_data, msg_id = get_automation_config(ws, auto_entity_id, msg_id)
        if not config_data:
            print(f"Could not fetch config for {auto_entity_id}")
            continue

        config_str = json.dumps(config_data)

        modified = False
        for old_id, new_id in replacements:
            pattern = re.compile(re.escape(old_id) + r"(?![a-z0-9_.-])", re.IGNORECASE)

            if pattern.search(config_str):
                config_str = pattern.sub(new_id, config_str)
                modified = True
                if execute:
                    print(
                        f"  Updating reference {old_id} -> {new_id} in {auto_entity_id}"
                    )

        if modified:
            if execute:
                new_config = json.loads(config_str)
                if save_automation_config(new_config):
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


def get_device_registry(ws, msg_id):
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "config/device_registry/list"}))
    result = ws.recv()
    result = json.loads(result)

    devices = {}
    if result["success"]:
        for d in result["result"]:
            devices[d["id"]] = d
    else:
        print("Failed to list devices.")

    return devices, msg_id


def process_entities(ws, entities, execute=False, recreate_ids=True, verbose=False):
    if not entities:
        return

    msg_id = 100  # Start safely above list_entities id

    # Fetch device registry to handle custom device names
    devices, msg_id = get_device_registry(ws, msg_id)

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
        msg_id = update_automation_references(ws, updates, msg_id, execute=execute)

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
                if default_device_name and original_name.startswith(
                    default_device_name
                ):
                    suffix = original_name[len(default_device_name) :].strip()
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
        table = [headers] + align_strings(table_data)
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
            msg_id = update_automation_references(ws, updates, msg_id, execute=True)


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


def connect_websocket():
    websocket_url = f"ws{TLS_S}://{config.HOST}/api/websocket"
    sslopt = {"cert_reqs": ssl.CERT_NONE} if not config.SSL_VERIFY else {}
    ws = websocket.WebSocket(sslopt=sslopt)
    ws.connect(websocket_url)

    auth_req = ws.recv()

    # Authenticate with Home Assistant
    auth_msg = json.dumps({"type": "auth", "access_token": config.ACCESS_TOKEN})
    ws.send(auth_msg)
    auth_result = ws.recv()
    auth_result = json.loads(auth_result)
    if auth_result["type"] != "auth_ok":
        print("Authentication failed. Check your access token.")
        ws.close()
        return None
    return ws


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
    args = parser.parse_args()

    ws = connect_websocket()
    if ws:
        try:
            entities = list_entities(ws, args.search_regex)
            process_entities(
                ws, entities, args.execute, args.recreate_ids, args.verbose
            )
        finally:
            ws.close()
