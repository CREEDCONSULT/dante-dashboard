#!/usr/bin/env python3
"""Import 149 schedule tasks into Linear with proper labels and descriptions."""
import json, os, subprocess, sys, time

LINEAR_KEY = os.environ.get("LINEAR_API_KEY", "")
LINEAR_URL = "https://api.linear.app/graphql"
TEAM_ID = "336004e4-f615-40c8-8bf5-c8a8827e51ff"

# Category → label mapping
CATEGORY_LABEL_MAP = {
    "health": "HEALTH",
    "spiritual": "SPIRITUAL",
    "free": "FREE TIME",
    "meals": "MEALS/BREAKS",
    "trading": "TRADING",
    "volatile": "VOLATILE",
    "networking": "NETWORKING",
    "planning": "PLANNING",
    "avatar": "AI AVATAR",
    "admin": "ADMIN",
    "creed": "CREED CONSULT",
    "tradingbot": "TRADING BOT",
    "product": "DIGITAL PRODUCT",
}

# Category colors for labels
CATEGORY_COLORS = {
    "health": "#FF4444",
    "spiritual": "#BBAACC",
    "free": "#333344",
    "meals": "#555555",
    "trading": "#00C49F",
    "volatile": "#FF6B35",
    "networking": "#00BCD4",
    "planning": "#FFBB28",
    "avatar": "#FFBB28",
    "admin": "#666688",
    "creed": "#AF19FF",
    "tradingbot": "#0088FE",
    "product": "#FF4081",
}

def linear(query, variables=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    r = subprocess.run(
        ["curl", "-s", "-X", "POST", LINEAR_URL,
         "-H", f"Authorization: {LINEAR_KEY}",
         "-H", "Content-Type: application/json",
         "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        return json.loads(r.stdout)
    except:
        print(f"  ❌ Linear API error: {r.stdout[:200]}")
        return {}

def get_or_create_labels():
    """Fetch existing labels and create missing ones."""
    print("📋 Syncing labels...")
    resp = linear(
        'query { issueLabels { nodes { id name } } }'
    )
    existing = {}
    for node in resp.get("data", {}).get("issueLabels", {}).get("nodes", []):
        existing[node["name"].upper()] = node["id"]

    for cat_key, label_name in CATEGORY_LABEL_MAP.items():
        if label_name.upper() not in existing:
            color = CATEGORY_COLORS.get(cat_key, "#666666")
            print(f"  Creating label: {label_name}")
            resp = linear(
                'mutation($input: IssueLabelCreateInput!) { issueLabelCreate(input: $input) { success issueLabel { id name } } }',
                {"input": {"name": label_name, "color": color}},
            )
            data = resp.get("data", {}).get("issueLabelCreate", {})
            if data.get("success"):
                existing[label_name.upper()] = data["issueLabel"]["id"]
            else:
                print(f"  ⚠️ Failed: {resp.get('errors', 'unknown')}")
        else:
            pass  # Label exists

    return existing

def create_issue(task, label_ids):
    """Create a single Linear issue. Skip meals/free tasks."""
    cat = task["category"]
    if cat in ("meals", "free", "planning"):
        return "SKIP"  # Don't import lifestyle blocks

    label_name = CATEGORY_LABEL_MAP.get(cat, "ADMIN")
    label_id = label_ids.get(label_name.upper(), "")

    title = f"[{label_name}] {task['title']}"
    day = task["day"]
    day_name = time.strftime("%A", time.strptime(day, "%Y-%m-%d"))
    desc = (
        f"**{day_name}, {day}** | {task['startTime']}–{task['endTime']} ({task.get('duration_mins', '?')} min)\n\n"
        f"{task['details']}"
    )

    # Determine initial state based on day
    # Past days → backlog, Today/Tomorrow → backlog (to be manually started)
    today = time.strftime("%Y-%m-%d")
    if day < today:
        state_id = None  # leave as default (backlog)
    else:
        state_id = None  # backlog

    resp = linear(
        'mutation($input: IssueCreateInput!) { issueCreate(input: $input) { success issue { id identifier } } }',
        {
            "input": {
                "teamId": TEAM_ID,
                "title": title,
                "description": desc,
                "labelIds": [label_id] if label_id else [],
            }
        },
    )

    data = resp.get("data", {}).get("issueCreate", {})
    if data.get("success"):
        return data["issue"]["identifier"]
    else:
        err = resp.get("errors", [{}])[0].get("message", "unknown")
        return f"FAIL: {err[:80]}"

def main():
    with open("/home/creed/.hermes/cache/documents/doc_9a1cd3b5f08e_dashboard_schedule_data.json") as f:
        tasks = json.load(f)

    label_ids = get_or_create_labels()

    tasks_to_import = [t for t in tasks if t["category"] not in ("meals", "free", "planning")]
    print(f"\n🚀 Importing {len(tasks_to_import)} tasks (skipping {len(tasks) - len(tasks_to_import)} meals/free/planning)...\n")

    created = 0
    skipped = 0
    failed = 0

    for i, task in enumerate(tasks_to_import):
        cat = task["category"]
        result = create_issue(task, label_ids)
        if result == "SKIP":
            skipped += 1
            continue
        if result.startswith("FAIL"):
            failed += 1
            print(f"  [{i+1}/{len(tasks_to_import)}] ❌ {task['day']} [{cat}] {task['title'][:50]}: {result}")
        else:
            created += 1
            print(f"  [{i+1}/{len(tasks_to_import)}] ✅ {result}: {task['day']} [{cat}] {task['title'][:50]}")

        # Rate limit: max ~60 requests/minute for safety
        if (i + 1) % 15 == 0:
            time.sleep(2)

    print(f"\n✅ Done! Created: {created}, Skipped: {skipped}, Failed: {failed}")

if __name__ == "__main__":
    main()