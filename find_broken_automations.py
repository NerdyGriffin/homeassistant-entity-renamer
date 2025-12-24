#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

import argparse
import json
import re
import tabulate
import common
import argcomplete

tabulate.PRESERVE_WHITESPACE = True


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
    config_data, msg_id = common.get_automation_config(ws, automation_entity_id, msg_id)
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

    # Use common.replace_references for safe replacement
    if common.replace_references(config_data, old_ref, new_ref):
        if common.save_automation_config(config_data):
            print(f"  Successfully updated {automation_entity_id}")
        else:
            print(f"  Failed to save {automation_entity_id}")
    else:
        print(
            f"  Could not find exact match for {old_ref} in {automation_entity_id} config."
        )

    return msg_id


def find_broken_references(ws, verbose=False, fix=False):
    print("Fetching entities and services...")
    msg_id = 1
    valid_entities, msg_id = common.get_valid_entities(ws, msg_id)
    valid_services, msg_id = common.get_valid_services(ws, msg_id)

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
        config_data, msg_id = common.get_automation_config(ws, auto_id, msg_id)
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

            if common.is_ignored(match):
                continue

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
                suggestions = common.suggest_fix(broken_ref, valid_set)
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
        return True
    else:
        print("\nNo broken references found.")
        return False


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
    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    ws = common.connect_websocket()
    if ws:
        try:
            if find_broken_references(ws, args.verbose, args.fix):
                import sys

                sys.exit(1)
        finally:
            ws.close()
