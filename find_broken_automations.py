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


def get_valid_services(ws, msg_id):
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "get_services"}))
    result = ws.recv()
    result = json.loads(result)

    if not result["success"]:
        print("Failed to list services.")
        return set(), msg_id

    services = set()
    for domain, domain_services in result["result"].items():
        for service in domain_services:
            services.add(f"{domain}.{service}")
    return services, msg_id


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


def suggest_fix(broken_ref, valid_entities):
    if "." not in broken_ref:
        return []

    domain, name = broken_ref.split(".", 1)
    suggestions = []

    # 1. Fuzzy matching using difflib
    # Filter valid entities to only those in the same domain to improve accuracy
    same_domain_entities = [e for e in valid_entities if e.startswith(f"{domain}.")]

    matches = difflib.get_close_matches(
        broken_ref, same_domain_entities, n=3, cutoff=0.6
    )
    suggestions.extend(matches)

    # 2. Common suffixes that might have been removed during a reset
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

    # Try removing suffixes
    for suffix in suffixes:
        if name.endswith(suffix):
            new_name = name[: -len(suffix)]
            candidate = f"{domain}.{new_name}"
            if candidate in valid_entities:
                suggestions.append(candidate)

    # Deduplicate while preserving order
    seen = set()
    unique_suggestions = []
    for s in suggestions:
        if s not in seen:
            unique_suggestions.append(s)
            seen.add(s)

    return unique_suggestions


def get_automation_id(ws, entity_id, msg_id):
    msg_id += 1
    ws.send(
        json.dumps(
            {"id": msg_id, "type": "config/entity_registry/get", "entity_id": entity_id}
        )
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        # The 'unique_id' in the registry is often the automation ID used for config
        return result["result"]["unique_id"], msg_id
    return None, msg_id


def apply_fix(ws, automation_entity_id, old_ref, new_ref, msg_id):
    config_data, msg_id = get_automation_config(ws, automation_entity_id, msg_id)
    if not config_data:
        print(f"Could not fetch config for {automation_entity_id}")
        return msg_id

    # Ensure ID is present
    if "id" not in config_data:
        # Try to fetch the ID from the registry
        unique_id, msg_id = get_automation_id(ws, automation_entity_id, msg_id)
        if unique_id:
            config_data["id"] = unique_id
        else:
            print(
                f"  Could not determine ID for {automation_entity_id}. Skipping save."
            )
            return msg_id

    config_str = json.dumps(config_data)

    # Safe replacement using regex to ensure we don't replace partial words
    pattern = re.compile(re.escape(old_ref) + r"(?![a-z0-9_.-])", re.IGNORECASE)

    if pattern.search(config_str):
        new_config_str = pattern.sub(new_ref, config_str)
        new_config = json.loads(new_config_str)

        if save_automation_config(new_config):
            print(f"  Successfully updated {automation_entity_id}")
        else:
            print(f"  Failed to save {automation_entity_id}")
    else:
        print(
            f"  Could not find exact match for {old_ref} in {automation_entity_id} config string."
        )

    return msg_id


def get_automation_config(ws, entity_id, msg_id):
    msg_id += 1
    ws.send(
        json.dumps({"id": msg_id, "type": "automation/config", "entity_id": entity_id})
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        # The automation config is wrapped in a "config" key
        if "config" in result["result"]:
            return result["result"]["config"], msg_id
        return result["result"], msg_id
    return None, msg_id


def find_broken_references(ws, verbose=False, fix=False):
    print("Fetching entities and services...")
    msg_id = 1
    valid_entities, msg_id = get_valid_entities(ws, msg_id)
    valid_services, msg_id = get_valid_services(ws, msg_id)

    # Also include some common special values or domains that might appear
    # e.g. 'homeassistant.turn_on' is a service, so it's covered.
    # 'sun.sun' is an entity.

    valid_set = valid_entities | valid_services

    print(f"Found {len(valid_entities)} entities and {len(valid_services)} services.")

    automations = [e for e in valid_entities if e.startswith("automation.")]
    print(f"Scanning {len(automations)} automations for broken references...")

    broken_refs = []

    # Helper to classify reference
    def is_likely_service(ref):
        if "." not in ref:
            return False
        domain, name = ref.split(".", 1)

        # Known service domains
        if domain in [
            "homeassistant",
            "system_log",
            "logger",
            "persistent_notification",
            "notify",
            "tts",
            "frontend",
            "recorder",
            "history",
            "logbook",
        ]:
            return True

        # Common service verbs
        verbs = [
            "turn_on",
            "turn_off",
            "toggle",
            "stop",
            "start",
            "restart",
            "reload",
            "create",
            "delete",
            "add_item",
            "remove_item",
            "snapshot",
            "play_media",
            "trigger",
        ]
        if name in verbs:
            return True

        return False

    for auto_id in automations:
        config_data, msg_id = get_automation_config(ws, auto_id, msg_id)
        if not config_data:
            if verbose:
                print(f"Skipping {auto_id}: Could not fetch config.")
            continue

        config_str = json.dumps(config_data)

        # Regex to find potential entity_ids or service calls
        # Pattern: word.word (where word is alphanumeric + underscore)
        # We exclude keys in JSON by ensuring it's not followed by ":" (roughly)
        # But JSON keys are quoted. "key": "value".
        # We want to find "value" that looks like "domain.name".

        # Simple regex: matches "domain.name" inside quotes
        matches = re.findall(r'"([a-z0-9_]+\.[a-z0-9_]+)"', config_str, re.IGNORECASE)

        for match in matches:
            # Filter out common false positives
            if match == auto_id:
                continue  # Self reference (id field)
            if match in [
                "platform.state",
                "platform.numeric_state",
                "platform.template",
                "platform.time",
                "platform.sun",
                "platform.zone",
                "platform.webhook",
                "platform.mqtt",
            ]:
                continue
            if match.startswith("input_select."):
                continue  # Options might look like IDs? No, usually not dot separated unless value is an ID.

            if match not in valid_set:
                broken_refs.append((auto_id, match))
                if verbose:
                    print(f"  {auto_id}: Potential broken reference '{match}'")

    if broken_refs:
        missing_services = []
        missing_entities = []

        for auto_id, ref in broken_refs:
            if is_likely_service(ref):
                missing_services.append((auto_id, ref))
            else:
                missing_entities.append((auto_id, ref))

        if missing_entities:
            print("\nPotential Missing Entities:")
            print(
                tabulate.tabulate(
                    missing_entities,
                    headers=["Automation", "Missing Entity"],
                    tablefmt="github",
                )
            )

        if missing_services:
            print("\nPotential Missing Services:")
            print(
                tabulate.tabulate(
                    missing_services,
                    headers=["Automation", "Missing Service"],
                    tablefmt="github",
                )
            )

        if fix and missing_entities:
            print("\nAttempting to fix broken entity references...")
            for auto_id, broken_ref in missing_entities:
                suggestions = suggest_fix(broken_ref, valid_set)
                if suggestions:
                    print(f"\nFound potential fix for '{broken_ref}' in '{auto_id}':")
                    for i, suggestion in enumerate(suggestions, 1):
                        print(f"  {i}. {suggestion}")

                    answer = input(f"  Apply a fix? (1-{len(suggestions)}/N): ")
                    if answer.isdigit() and 1 <= int(answer) <= len(suggestions):
                        selected_fix = suggestions[int(answer) - 1]
                        msg_id = apply_fix(
                            ws, auto_id, broken_ref, selected_fix, msg_id
                        )
                    else:
                        print("  Skipped.")
    else:
        print("\nNo broken references found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find Broken Automation References")
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
            find_broken_references(ws, args.verbose, args.fix)
        finally:
            ws.close()
