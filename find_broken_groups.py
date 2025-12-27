#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

import argparse
import json
import tabulate
import common
import argcomplete

tabulate.PRESERVE_WHITESPACE = True


def get_all_states(ws, msg_id):
    msg_id += 1
    ws.send(json.dumps({"id": msg_id, "type": "get_states"}))
    result = ws.recv()
    result = json.loads(result)
    if result["success"]:
        return result["result"], msg_id
    return [], msg_id


def update_group(ws, object_id, members, msg_id):
    # This only works for 'group' domain legacy groups
    msg_id += 1
    ws.send(
        json.dumps(
            {
                "id": msg_id,
                "type": "call_service",
                "domain": "group",
                "service": "set",
                "service_data": {"object_id": object_id, "entities": members},
            }
        )
    )
    result = ws.recv()
    result = json.loads(result)
    return result["success"], msg_id


def find_broken_groups(ws, verbose=False, fix=False):
    print("Fetching entities...")
    msg_id = 1
    valid_entities, msg_id = common.get_valid_entities(ws, msg_id)
    print(f"Found {len(valid_entities)} valid entities.")

    print("Fetching states...")
    states, msg_id = get_all_states(ws, msg_id)

    groups = []
    for state in states:
        attrs = state.get("attributes", {})
        if "entity_id" in attrs and isinstance(attrs["entity_id"], list):
            groups.append(state)

    print(f"Scanning {len(groups)} groups/entities with members...")

    broken_groups = []

    for group in groups:
        entity_id = group["entity_id"]
        members = group["attributes"]["entity_id"]

        broken_members = [m for m in members if m not in valid_entities]

        if broken_members:
            broken_groups.append(
                {
                    "entity_id": entity_id,
                    "members": members,
                    "broken": broken_members,
                }
            )
            if verbose:
                print(f"  {entity_id}: Broken members: {broken_members}")

    if broken_groups:
        print(f"\nFound {len(broken_groups)} groups with broken members:")
        table_data = []
        for bg in broken_groups:
            for broken in bg["broken"]:
                table_data.append((bg["entity_id"], broken))

        print(
            tabulate.tabulate(
                table_data, headers=["Group", "Missing Member"], tablefmt="github"
            )
        )

        if fix:
            print("\nAttempting to fix broken groups...")
            for bg in broken_groups:
                entity_id = bg["entity_id"]
                domain = entity_id.split(".")[0]

                # Check if it's a config entry helper
                registry_entry, msg_id = common.get_registry_entry(
                    ws, entity_id, msg_id
                )
                config_entry_id = (
                    registry_entry.get("config_entry_id") if registry_entry else None
                )

                if not config_entry_id and domain != "group":
                    print(
                        f"  Skipping {entity_id}: Auto-fix only supported for 'group' domain or Helper entities."
                    )
                    continue

                # Determine if we can fix it
                can_fix = False
                if domain == "group" and not config_entry_id:
                    can_fix = True  # Legacy group
                elif config_entry_id:
                    can_fix = True  # Helper (attempt)

                if not can_fix:
                    print(f"  Skipping {entity_id}: Cannot determine how to update.")
                    continue

                current_members = list(bg["members"])
                modified = False

                for broken in bg["broken"]:
                    suggestions = common.suggest_fix(broken, valid_entities)
                    if suggestions:
                        print(f"\nFound potential fix for '{broken}' in '{entity_id}':")
                        for i, suggestion in enumerate(suggestions, 1):
                            print(f"  {i}. {suggestion}")

                        answer = common.prompt_apply_fix(len(suggestions))
                        # Note: This prompt supports additional 'd' for delete option
                        if answer.isdigit() and 1 <= int(answer) <= len(suggestions):
                            selected_fix = suggestions[int(answer) - 1]
                            # Replace in list
                            current_members = [
                                selected_fix if m == broken else m
                                for m in current_members
                            ]
                            modified = True
                            print(f"  Replacing {broken} with {selected_fix}")
                        elif answer.lower() == "d":
                            current_members = [
                                m for m in current_members if m != broken
                            ]
                            modified = True
                            print(f"  Removing {broken}")
                        else:
                            print("  Skipped.")
                    else:
                        print(f"\nNo suggestions for '{broken}' in '{entity_id}'.")
                        answer = common.prompt_delete_member()
                        if answer.lower() == "y":
                            current_members = [
                                m for m in current_members if m != broken
                            ]
                            modified = True
                            print(f"  Removing {broken}")

                if modified:
                    if config_entry_id:
                        # Update config entry
                        # We assume the key is 'entities' for group-like helpers
                        # Note: This might fail if the integration doesn't support config_entries/update
                        success, msg_id = common.update_config_entry_options(
                            ws, config_entry_id, {"entities": current_members}, msg_id
                        )
                        if success:
                            print(
                                f"  Successfully updated config entry for {entity_id}"
                            )
                        else:
                            print(
                                f"  Failed to update config entry for {entity_id}. Please update via UI."
                            )
                    else:
                        # Legacy group update
                        object_id = entity_id.split(".", 1)[1]
                        success, msg_id = update_group(
                            ws, object_id, current_members, msg_id
                        )
                        if success:
                            print(f"  Successfully updated {entity_id}")
                        else:
                            print(f"  Failed to update {entity_id}")
        return True

    else:
        print("\nNo broken groups found.")
        return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Find Broken Group Members")
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

    with common.websocket_context() as ws:
        if ws:
            if find_broken_groups(ws, args.verbose, args.fix):
                import sys

                sys.exit(1)
