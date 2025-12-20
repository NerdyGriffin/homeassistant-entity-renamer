#!/usr/bin/env python3

import json
import tabulate
from collections import defaultdict
import common

tabulate.PRESERVE_WHITESPACE = True


def list_platforms():
    ws = common.connect_websocket()
    if not ws:
        return

    # List registry entries
    msg_id = 1
    ws.send(json.dumps({"id": msg_id, "type": "config/entity_registry/list"}))
    result = ws.recv()
    result = json.loads(result)

    if result["success"]:
        entities = result["result"]

        platform_counts = defaultdict(int)
        platform_examples = defaultdict(list)

        for e in entities:
            platform = e.get("platform")
            platform_counts[platform] += 1
            if len(platform_examples[platform]) < 3:
                platform_examples[platform].append(e["entity_id"])

        # Prepare table
        table_data = []
        for platform, count in sorted(
            platform_counts.items(), key=lambda item: item[1], reverse=True
        ):
            examples = ", ".join(platform_examples[platform])
            table_data.append((platform, count, examples))

        print(
            tabulate.tabulate(
                table_data, headers=["Platform", "Count", "Examples"], tablefmt="github"
            )
        )

    else:
        print("Failed to list entities.")

    ws.close()


if __name__ == "__main__":
    list_platforms()
