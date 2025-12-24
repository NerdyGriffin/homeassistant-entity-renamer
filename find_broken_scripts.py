#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

import argparse
import json
import re
import tabulate
import common
import argcomplete

tabulate.PRESERVE_WHITESPACE = True


def get_script_id(ws, entity_id, msg_id):
    msg_id += 1
    ws.send(
        json.dumps(
            {"id": msg_id, "type": "config/entity_registry/get", "entity_id": entity_id}
        )
    )
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        return result["result"]["unique_id"], msg_id
    return None, msg_id


def apply_fix(ws, script_entity_id, old_ref, new_ref, msg_id):
    config_data, msg_id = common.get_script_config(ws, script_entity_id, msg_id)
    if not config_data:
        print(f"Could not fetch config for {script_entity_id}")
        return msg_id

    # Ensure ID is present for saving
    if "unique_id" not in config_data:
        unique_id, msg_id = get_script_id(ws, script_entity_id, msg_id)
        if unique_id:
            config_data["unique_id"] = unique_id
        else:
            print(
                f"  Could not determine unique_id for {script_entity_id}. Skipping save."
            )
            return msg_id

    # Use common.replace_references for safe replacement
    if common.replace_references(config_data, old_ref, new_ref):
        if common.save_script_config(config_data):
            print(f"  Successfully updated {script_entity_id}")
        else:
            print(f"  Failed to save {script_entity_id}")
    else:
        print(
            f"  Could not find exact match for {old_ref} in {script_entity_id} config."
        )

    return msg_id


def find_broken_references(ws, verbose=False, fix=False):
    print("Fetching entities and services...")
    msg_id = 1
    valid_entities, msg_id = common.get_valid_entities(ws, msg_id)
    valid_services, msg_id = common.get_valid_services(ws, msg_id)

    valid_set = valid_entities | valid_services

    print(f"Found {len(valid_entities)} entities and {len(valid_services)} services.")

    scripts = [e for e in valid_entities if e.startswith("script.")]
    print(f"Scanning {len(scripts)} scripts for broken references...")

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

    for script_id in scripts:
        config_data, msg_id = common.get_script_config(ws, script_id, msg_id)
        if not config_data:
            if verbose:
                print(f"Skipping {script_id}: Could not fetch config.")
            continue

        config_str = json.dumps(config_data)

        # Regex to find potential entity_ids or service calls
        matches = re.findall(r'"([a-z0-9_]+\.[a-z0-9_]+)"', config_str, re.IGNORECASE)

        for match in matches:
            # Filter out common false positives
            if match == script_id:
                continue
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
                continue

            if match not in valid_set:
                broken_refs.append((script_id, match))
                if verbose:
                    print(f"  {script_id}: Potential broken reference '{match}'")

    if broken_refs:
        missing_services = []
        missing_entities = []

        for script_id, ref in broken_refs:
            if is_likely_service(ref):
                missing_services.append((script_id, ref))
            else:
                missing_entities.append((script_id, ref))

        if missing_entities:
            print("\nPotential Missing Entities:")
            print(
                tabulate.tabulate(
                    missing_entities,
                    headers=["Script", "Missing Entity"],
                    tablefmt="github",
                )
            )

        if missing_services:
            print("\nPotential Missing Services:")
            print(
                tabulate.tabulate(
                    missing_services,
                    headers=["Script", "Missing Service"],
                    tablefmt="github",
                )
            )

        if fix and missing_entities:
            print("\nAttempting to fix broken entity references...")
            for script_id, broken_ref in missing_entities:
                suggestions = common.suggest_fix(broken_ref, valid_set)
                if suggestions:
                    print(f"\nFound potential fix for '{broken_ref}' in '{script_id}':")
                    for i, suggestion in enumerate(suggestions, 1):
                        print(f"  {i}. {suggestion}")

                    answer = input(f"  Apply a fix? (1-{len(suggestions)}/N): ")
                    if answer.isdigit() and 1 <= int(answer) <= len(suggestions):
                        selected_fix = suggestions[int(answer) - 1]
                        msg_id = apply_fix(
                            ws, script_id, broken_ref, selected_fix, msg_id
                        )
                    else:
                        print("  Skipped.")
    else:
        print("\nNo broken references found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find Broken Script References")
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
            find_broken_references(ws, args.verbose, args.fix)
        finally:
            ws.close()
