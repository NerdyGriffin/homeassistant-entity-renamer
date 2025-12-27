#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

import argparse
import json
import re
import tabulate
import common
import argcomplete

tabulate.PRESERVE_WHITESPACE = True


def find_entity_references(data, valid_entities):
    """Recursively find all strings that look like entity IDs."""
    refs = []

    if isinstance(data, dict):
        for key, value in data.items():
            # Check if the value itself is a string that looks like an entity ID
            if isinstance(value, str):
                # Heuristic: domain.name, no spaces, lowercase
                if re.match(common.ENTITY_ID_PATTERN, value):
                    # Exclude known non-entities
                    if value not in ["type", "icon", "name", "theme", "url_path"]:
                        refs.append(value)

            # Recurse
            refs.extend(find_entity_references(value, valid_entities))

    elif isinstance(data, list):
        for item in data:
            refs.extend(find_entity_references(item, valid_entities))

    return refs


def find_broken_dashboards(ws, verbose=False, fix=False, target_dashboard=None):
    print("Fetching entities...")
    msg_id = 1
    valid_entities, msg_id = common.get_valid_entities(ws, msg_id)
    print(f"Found {len(valid_entities)} valid entities.")

    print("Fetching dashboards...")
    dashboards, msg_id = common.list_dashboards(ws, msg_id)

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

    found_issues = False
    for dashboard in dashboards:
        url_path = dashboard.get("url_path")
        title = dashboard.get("title", "Unknown")

        config_data, msg_id = common.get_dashboard_config(ws, url_path, msg_id)
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
            if common.is_ignored(ref):
                continue

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
            found_issues = True
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
                    suggestions = common.suggest_fix(broken_ref, valid_entities)
                    if suggestions:
                        print(f"\nFound potential fix for '{broken_ref}':")
                        for i, suggestion in enumerate(suggestions, 1):
                            print(f"  {i}. {suggestion}")

                        answer = common.prompt_apply_fix(len(suggestions))
                        if answer.isdigit() and 1 <= int(answer) <= len(suggestions):
                            selected_fix = suggestions[int(answer) - 1]

                            if common.replace_references(
                                config_data, broken_ref, selected_fix
                            ):
                                print(f"  Updated config in memory.")
                                # Save immediately? Or batch?
                                # Let's save immediately to be safe.
                                success, msg_id = common.save_dashboard_config(
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

    return found_issues


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
    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    with common.websocket_context() as ws:
        if ws:
            if find_broken_dashboards(ws, args.verbose, args.fix, args.dashboard):
                import sys

                sys.exit(1)
