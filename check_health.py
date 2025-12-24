#!/usr/bin/env python3
# PYTHON_ARGCOMPLETE_OK

import argparse
import subprocess
import sys
import argcomplete
import tabulate


def run_check(script_name, description, fix=False, verbose=False):
    print(f"\n{'='*60}")
    print(f"Running: {description}")
    print(f"{'='*60}")

    cmd = ["./" + script_name]
    if fix:
        cmd.append("--fix")
    if verbose:
        cmd.append("--verbose")

    try:
        result = subprocess.run(cmd, check=False)
        return result.returncode
    except FileNotFoundError:
        print(f"Error: Script {script_name} not found.")
        return -1
    except Exception as e:
        print(f"Error running {script_name}: {e}")
        return -1


def main():
    parser = argparse.ArgumentParser(description="Run all Home Assistant health checks")
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

    checks = [
        ("find_broken_automations.py", "Checking Automations"),
        ("find_broken_scripts.py", "Checking Scripts"),
        ("find_broken_groups.py", "Checking Groups & Helpers"),
        ("find_broken_dashboards.py", "Checking Dashboards"),
    ]

    results = []
    for script, desc in checks:
        code = run_check(script, desc, args.fix, args.verbose)
        results.append((desc, code))

    print(f"\n{'='*60}")
    print("Health Check Summary")
    print(f"{'='*60}")

    summary_data = []
    for desc, code in results:
        if code == 0:
            status = "PASS"
        elif code == 1:
            status = "FAIL"  # Issues found
        else:
            status = "ERROR"
        summary_data.append([desc, status])

    print(
        tabulate.tabulate(summary_data, headers=["Check", "Status"], tablefmt="github")
    )


if __name__ == "__main__":
    main()
