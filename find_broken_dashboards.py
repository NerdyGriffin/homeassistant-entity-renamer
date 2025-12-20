#!/usr/bin/env python3

import argparse
import config
import json
import re
import requests
import ssl
import tabulate
import websocket
import difflib

tabulate.PRESERVE_WHITESPACE = True

# Determine the protocol based on TLS configuration
TLS_S = "s" if config.TLS else ""


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


def get_valid_entities(ws, msg_id):
    # Get registry entities
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "config/entity_registry/list"}))
    result = ws.recv()
    result = json.loads(result)

    entities = set()
    if result["success"]:
        entities = {e["entity_id"] for e in result["result"]}
    else:
        print("Failed to list registry entities.")

    # Get state entities (includes non-registry items like zone.home, sun.sun)
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "get_states"}))
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        for e in result["result"]:
            entities.add(e["entity_id"])
    else:
        print("Failed to list states.")

    return entities, msg_id


def list_dashboards(ws, msg_id):
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "lovelace/dashboards/list"}))
    result = ws.recv()
    result = json.loads(result)

    dashboards = []
    if result["success"]:
        dashboards = result["result"]

    # Add the default dashboard (null url_path usually implies default, but we treat it specially)
    # Actually, the default dashboard is usually not in this list if it's auto-generated,
    # but if it's in storage mode, it might be accessible via 'lovelace/config' without url_path.
    # We will handle the default dashboard separately.

    return dashboards, msg_id


def get_dashboard_config(ws, url_path, msg_id):
    msg_id += 1
    payload = {"id": msg_id, "type": "lovelace/config"}
    if url_path:
        payload["url_path"] = url_path

    ws.send(json.dumps(payload))
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        return result["result"], msg_id
    return None, msg_id


def save_dashboard_config(ws, url_path, config_data, msg_id):
    msg_id += 1
    payload = {"id": msg_id, "type": "lovelace/config/save", "config": config_data}
    if url_path:
        payload["url_path"] = url_path

    ws.send(json.dumps(payload))
    result = ws.recv()
    result = json.loads(result)

    return result["success"], msg_id


def suggest_fix(broken_ref, valid_entities):
    if "." not in broken_ref:
        return []

    domain, name = broken_ref.split(".", 1)
    suggestions = []

    # 1. Fuzzy matching using difflib
    same_domain_entities = [e for e in valid_entities if e.startswith(f"{domain}.")]
    matches = difflib.get_close_matches(
        broken_ref, same_domain_entities, n=3, cutoff=0.6
    )
    suggestions.extend(matches)

    # 2. Common suffixes
    suffixes = [
        "_switch",
        "_light",
        "_sensor",
        "_binary_sensor",
        "_cover",
        "_fan",
        "_lock",
        "_climate",
        "_media_player",
    ]
    for suffix in suffixes:
        if name.endswith(suffix):
            new_name = name[: -len(suffix)]
            candidate = f"{domain}.{new_name}"
            if candidate in valid_entities:
                suggestions.append(candidate)

    # Deduplicate
    seen = set()
    unique_suggestions = []
    for s in suggestions:
        if s not in seen:
            unique_suggestions.append(s)
            seen.add(s)

    return unique_suggestions


def find_entity_references(data, valid_entities):
    """Recursively find all strings that look like entity IDs."""
    refs = []

    if isinstance(data, dict):
        for key, value in data.items():
            # Check if the value itself is a string that looks like an entity ID
            if isinstance(value, str):
                # Heuristic: domain.name, no spaces, lowercase
                if re.match(r"^[a-z0-9_]+\.[a-z0-9_]+$", value):
                    # Exclude known non-entities
                    if value not in ["type", "icon", "name", "theme", "url_path"]:
                        refs.append(value)

            # Recurse
            refs.extend(find_entity_references(value, valid_entities))

    elif isinstance(data, list):
        for item in data:
            refs.extend(find_entity_references(item, valid_entities))

    return refs


def replace_references(data, old_ref, new_ref):
    """Recursively replace references in the config object."""
    modified = False

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, str) and value == old_ref:
                data[key] = new_ref
                modified = True
            elif isinstance(value, (dict, list)):
                if replace_references(value, old_ref, new_ref):
                    modified = True

    elif isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, str) and item == old_ref:
                data[i] = new_ref
                modified = True
            elif isinstance(item, (dict, list)):
                if replace_references(item, old_ref, new_ref):
                    modified = True

    return modified


def find_broken_dashboards(ws, verbose=False, fix=False, target_dashboard=None):
    print("Fetching entities...")
    msg_id = 1
    valid_entities, msg_id = get_valid_entities(ws, msg_id)
    print(f"Found {len(valid_entities)} valid entities.")

    print("Fetching dashboards...")
    dashboards, msg_id = list_dashboards(ws, msg_id)

    # Add a pseudo-entry for the default dashboard
    dashboards.insert(
        0, {"url_path": None, "title": "Default (Overview)", "id": "default"}
    )

    if target_dashboard:
        # Filter dashboards by url_path or id
        dashboards = [
            d
            for d in dashboards
            if d.get("url_path") == target_dashboard or d.get("id") == target_dashboard
        ]
        if not dashboards:
            print(f"Dashboard '{target_dashboard}' not found.")
            return

    print(f"Scanning {len(dashboards)} dashboards...")

    for dashboard in dashboards:
        url_path = dashboard.get("url_path")
        title = dashboard.get("title", "Unknown")

        config_data, msg_id = get_dashboard_config(ws, url_path, msg_id)
        if not config_data:
            if verbose:
                print(
                    f"Skipping {title}: Could not fetch config (might be auto-generated)."
                )
            continue

        # Find all potential entity references
        refs = find_entity_references(config_data, valid_entities)

        # Filter for broken ones
        broken_refs = sorted(list(set([r for r in refs if r not in valid_entities])))

        # Filter out likely false positives (service calls, special keywords)
        # This is a bit heuristic.
        filtered_broken_refs = []
        for ref in broken_refs:
            domain = ref.split(".")[0]
            if domain in [
                "input_select",
                "input_text",
                "input_number",
                "input_boolean",
                "input_datetime",
                "input_button",
            ]:
                # Inputs are entities, so if they are missing, they are broken.
                pass
            elif domain in [
                "sensor",
                "binary_sensor",
                "switch",
                "light",
                "cover",
                "media_player",
                "climate",
                "fan",
                "lock",
                "camera",
                "weather",
                "device_tracker",
                "person",
                "zone",
                "sun",
                "timer",
                "counter",
                "group",
                "scene",
                "script",
                "automation",
            ]:
                # Standard domains
                pass
            else:
                # Likely a service call or other config value (e.g. 'custom:button-card')
                if verbose:
                    print(f"  Ignoring likely non-entity: {ref}")
                continue

            filtered_broken_refs.append(ref)

        if filtered_broken_refs:
            print(
                f"\nBroken references in dashboard '{title}' ({url_path or 'default'}):"
            )
            table_data = [(ref,) for ref in filtered_broken_refs]
            print(
                tabulate.tabulate(
                    table_data, headers=["Missing Entity"], tablefmt="github"
                )
            )

            if fix:
                for broken_ref in filtered_broken_refs:
                    suggestions = suggest_fix(broken_ref, valid_entities)
                    if suggestions:
                        print(f"\nFound potential fix for '{broken_ref}':")
                        for i, suggestion in enumerate(suggestions, 1):
                            print(f"  {i}. {suggestion}")

                        answer = input(f"  Apply a fix? (1-{len(suggestions)}/N): ")
                        if answer.isdigit() and 1 <= int(answer) <= len(suggestions):
                            selected_fix = suggestions[int(answer) - 1]

                            if replace_references(
                                config_data, broken_ref, selected_fix
                            ):
                                print(f"  Updated config in memory.")
                                # Save immediately? Or batch?
                                # Let's save immediately to be safe.
                                success, msg_id = save_dashboard_config(
                                    ws, url_path, config_data, msg_id
                                )
                                if success:
                                    print("  Successfully saved dashboard config.")
                                else:
                                    print("  Failed to save dashboard config.")
                            else:
                                print(
                                    "  Could not find reference in config structure (weird)."
                                )
                        else:
                            print("  Skipped.")
        elif verbose:
            print(f"No broken references found in '{title}'.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find Broken Dashboard References")
    parser.add_argument(
        "dashboard",
        nargs="?",
        help="Optional: Specific dashboard URL path or ID to scan (e.g. 'dashboard-christian')",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Show detailed progress"
    )
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Attempt to auto-fix broken references interactively",
    )
    args = parser.parse_args()

    ws = connect_websocket()
    if ws:
        try:
            find_broken_dashboards(ws, args.verbose, args.fix, args.dashboard)
        finally:
            ws.close()
