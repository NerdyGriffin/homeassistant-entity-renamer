#!/usr/bin/env python3

import argparse
import config
import csv
import json
import re
import requests
import tabulate
import common

tabulate.PRESERVE_WHITESPACE = True

# Determine the protocol based on TLS configuration
TLS_S = "s" if config.TLS else ""

# Header containing the access token
headers = {
    "Authorization": f"Bearer {config.ACCESS_TOKEN}",
    "Content-Type": "application/json",
}

def list_entities(regex=None):
    # API endpoint for retrieving all entities
    api_endpoint = f"http{TLS_S}://{config.HOST}/api/states"

    # Send GET request to the API endpoint
    response = requests.get(api_endpoint, headers=headers, verify=config.SSL_VERIFY)

    # Check if the request was successful
    if response.status_code == 200:
        # Parse the JSON response
        data = json.loads(response.text)

        # Extract entity IDs and friendly names
        entity_data = [
            (entity["attributes"].get("friendly_name", ""), entity["entity_id"])
            for entity in data
        ]

        # Filter the entity data if regex argument is provided
        if regex:
            filtered_entity_data = [
                (friendly_name, entity_id)
                for friendly_name, entity_id in entity_data
                if re.search(regex, entity_id)
            ]
            entity_data = filtered_entity_data

        # Sort the entity data by friendly name
        entity_data = sorted(entity_data, key=lambda x: x[0])

        # Output the entity data
        return entity_data

    else:
        print(f"Error: {response.status_code} - {response.text}")
        return []


def process_entities(entity_data, search_regex, replace_regex=None, output_csv=None):
    rename_data = []
    if replace_regex is not None:
        for friendly_name, entity_id in entity_data:
            new_entity_id = re.sub(search_regex, replace_regex, entity_id)
            rename_data.append((friendly_name, entity_id, new_entity_id))
    else:
        rename_data = [
            (friendly_name, entity_id, "") for friendly_name, entity_id in entity_data
        ]

    # Print the table with friendly name and entity ID
    table = [("Friendly Name", "Current Entity ID", "New Entity ID")] + common.align_strings(
        rename_data
    )
    print(tabulate.tabulate(table, headers="firstrow", tablefmt="github"))

    # Same table, but without whitespace for alignment
    table = [("Friendly Name", "Current Entity ID", "New Entity ID")] + rename_data
    if output_csv:
        with open(output_csv, "w", newline="") as csvfile:
            csv_writer = csv.writer(csvfile)
            csv_writer.writerows(table)
            print(f"(Table written to {output_csv})")

    # Ask user for confirmation if replace_regex is provided
    if replace_regex is None:
        return

    answer = input("\nDo you want to proceed with renaming the entities? (y/N): ")
    if answer.lower() not in ["y", "yes"]:
        print("Renaming process aborted.")
        return

    rename_entities(rename_data)


def rename_entities(rename_data):
    ws = common.connect_websocket()
    if not ws:
        return

    # Rename the entities
    msg_id = 1
    for index, (_, entity_id, new_entity_id) in enumerate(rename_data, start=1):
        msg_id += 1
        entity_registry_update_msg = json.dumps(
            {
                "id": msg_id,
                "type": "config/entity_registry/update",
                "entity_id": entity_id,
                "new_entity_id": new_entity_id,
            }
        )
        ws.send(entity_registry_update_msg)
        update_result = ws.recv()
        update_result = json.loads(update_result)
        if update_result["success"]:
            print(f"Entity '{entity_id}' renamed to '{new_entity_id}' successfully!")
        else:
            error_msg = update_result.get('error', {}).get('message', 'Unknown error')
            print(
                f"Failed to rename entity '{entity_id}': {error_msg}"
            )

    ws.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HomeAssistant Entity Renamer")
    parser.add_argument(
        "--search",
        dest="search_regex",
        help="Regular expression for search. Note: Only searches entity IDs.",
    )
    parser.add_argument(
        "--replace", dest="replace_regex", help="Regular expression for replace"
    )
    parser.add_argument(
        "--output-csv", dest="output_csv", help="Output preview table to CSV."
    )
    args = parser.parse_args()

    if args.search_regex:
        entity_data = list_entities(args.search_regex)

        if entity_data:
            process_entities(
                entity_data, args.search_regex, args.replace_regex, args.output_csv
            )
        else:
            print("No entities found matching the search regex.")
    else:
        parser.print_help()
