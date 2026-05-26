#!/usr/bin/env python3
"""
Dante Server v4 — Unified dashboard backend.
Serves the 7-day column SPA template, merges schedule data with Linear status,
proxies Linear mutations, caches Calendar data, runs briefings.
"""

import asyncio, json, os, re as _re, subprocess, sys, time
from datetime import datetime, timedelta, date
from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

# ── Config ──────────────────────────────────────────────────────
HERMES_HOME = Path(os.path.expanduser("~/.hermes"))
LINEAR_KEY = os.environ.get("LINEAR_API_KEY", "")
LINEAR_URL = "https://api.linear.app/graphql"
TEAM_ID = "336004e4-f615-40c8-8bf5-c8a8827e51ff"
STATE_FILE = HERMES_HOME / "dashboard_state.json"
DASHBOARD_TEMPLATE = HERMES_HOME / "templates" / "dashboard.html"
SCHEDULE_JSON = HERMES_HOME / "cache" / "documents" / "doc_9a1cd3b5f08e_dashboard_schedule_data.json"
GAPI = f"python3 {HERMES_HOME}/skills/productivity/google-workspace/scripts/google_api.py"
PORT = 8765
CACHE_TTL = 60

# ── Category config ─────────────────────────────────────────────
CATEGORY_LABEL_MAP = {
    "health": "HEALTH", "spiritual": "SPIRITUAL", "trading": "TRADING",
    "volatile": "VOLATILE", "creed": "CREED CONSULT", "tradingbot": "TRADING BOT",
    "avatar": "AI AVATAR", "product": "DIGITAL PRODUCT",
    "networking": "NETWORKING", "admin": "ADMIN", "planning": "PLANNING",
    "meals": "MEALS/BREAKS", "free": "FREE TIME",
}
CATEGORY_COLORS = {
    "health": "#FF4444", "spiritual": "#BBAACC", "trading": "#00C49F",
    "volatile": "#FF6B35", "creed": "#AF19FF", "tradingbot": "#0088FE",
    "avatar": "#FFBB28", "product": "#FF4081", "networking": "#00BCD4",
    "admin": "#666688", "planning": "#FFBB28", "meals": "#555555", "free": "#333344",
}
NON_ACTIONABLE = {"meals", "free"}
DASHBOARD_LABELS = {"HEALTH", "SPIRITUAL", "TRADING", "VOLATILE", "CREED CONSULT",
                    "TRADING BOT", "AI AVATAR", "DIGITAL PRODUCT", "NETWORKING", "ADMIN", "PLANNING"}

# ── Global cache ─────────────────────────────────────────────────
cache = {"issues": [], "calendar": [], "streak": 0, "last_fetch": 0}


# ═══════════════════════════════════════════════════════════════════
# LINEAR CLIENT
# ═══════════════════════════════════════════════════════════════════

async def linear(query: str, variables: dict = None) -> dict:
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            LINEAR_URL, json=payload,
            headers={"Authorization": LINEAR_KEY, "Content-Type": "application/json"},
        )
    return r.json()


async def fetch_issues() -> list[dict]:
    """Fetch all team issues from Linear."""
    q = (
        'query { issues(first: 100, filter: { team: { id: { eq: "%s" } } }) '
        "{ nodes { id identifier title state { name type } labels { nodes { name color } } url description } } }"
        % TEAM_ID
    )
    result = await linear(q)
    issues = []
    for node in result.get("data", {}).get("issues", {}).get("nodes", []):
        lbls = node.get("labels", {}).get("nodes", [])
        issues.append({
            "id": node["identifier"],
            "uuid": node["id"],
            "title": node["title"],
            "status": node["state"]["name"] if node.get("state") else "Unknown",
            "status_type": node["state"]["type"] if node.get("state") else "unknown",
            "labels": [l["name"] for l in lbls],
            "color": lbls[0]["color"] if lbls else "#666",
            "url": node["url"],
            "desc": node.get("description", "") or "",
        })
    return issues


# ═══════════════════════════════════════════════════════════════════
# DATA LOADING & MERGING
# ═══════════════════════════════════════════════════════════════════

def fetch_calendar_sync() -> list[dict]:
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    start = monday.strftime("%Y-%m-%d") + "T00:00:00-04:00"
    end = sunday.strftime("%Y-%m-%d") + "T23:59:59-04:00"
    try:
        r = subprocess.run(
            f'{GAPI} calendar list --start {start} --end {end}',
            shell=True, capture_output=True, text=True, timeout=15,
        )
        return json.loads(r.stdout)
    except Exception:
        return []


def load_schedule() -> list[dict]:
    """Load the 149-task schedule JSON → dashboard task format."""
    if not SCHEDULE_JSON.exists():
        return []
    with open(SCHEDULE_JSON) as f:
        raw = json.load(f)
    tasks = []
    for t in raw:
        tasks.append({
            "id": f"{t['day']}-{t['startTime']}-{t['category']}-{t['title'][:20].replace(' ','_')}",
            "title": t["title"],
            "details": t.get("details", ""),
            "category": t["category"],
            "day": t["day"],
            "startTime": t["startTime"],
            "endTime": t["endTime"],
            "status": "not_started",
            "labels": [CATEGORY_LABEL_MAP.get(t["category"], t["category"].upper())],
            "color": CATEGORY_COLORS.get(t["category"], "#666"),
        })
    return tasks


def merge_linear_status(schedule_tasks: list[dict], linear_issues: list[dict]) -> list[dict]:
    """Merge Linear issue statuses into schedule tasks by matching titles and day."""
    tag_strip = _re.compile(
        r"\[(VOLATILE|TRADING|TRADING BOT|AI AVATAR|CREED CONSULT|NETWORKING|"
        r"DIGITAL PRODUCT|HEALTH|SPIRITUAL|ADMIN|PLANNING|MEALS/BREAKS|FREE TIME)\]"
    )
    # Build lookup: (clean_title, day) → status_type
    linear_status = {}
    for li in linear_issues:
        clean = tag_strip.sub("", li["title"]).strip()
        desc = li.get("desc", "")
        # Extract day from description (format: "**Monday, 2026-05-25** | ...")
        day_match = _re.search(r"\*\*[A-Z][a-z]+day, (\d{4}-\d{2}-\d{2})\*\*", desc)
        day = day_match.group(1) if day_match else None
        key = (clean.lower(), day)
        linear_status[key] = li["status_type"]
        # Also index by title-only for fallback
        if (clean.lower(), None) not in linear_status:
            linear_status[(clean.lower(), None)] = li["status_type"]

    for t in schedule_tasks:
        clean_title = t["title"].lower()
        task_day = t["day"]
        # Try exact match (title + day), then fallback to title-only
        lt = linear_status.get((clean_title, task_day)) or linear_status.get((clean_title, None))
        if lt:
            if lt == "completed":
                t["status"] = "completed"
            elif lt in ("started", "in_progress"):
                t["status"] = "started"
            else:
                t["status"] = "not_started"
        else:
            t["status"] = "not_started"
    return schedule_tasks


# ═══════════════════════════════════════════════════════════════════
# STREAK TRACKING
# ═══════════════════════════════════════════════════════════════════

def load_streak() -> dict:
    if STATE_FILE.exists():
        return json.load(STATE_FILE.open())
    return {"streak": 0, "last_streak_date": None, "history": {}}

def save_streak(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=2))

def compute_streak(state: dict, issues: list[dict]) -> int:
    today_str = date.today().isoformat()
    now = datetime.now()
    if state.get("last_streak_date") == today_str:
        return state["streak"]

    daily_goals = [i for i in issues if any(l in {"HEALTH", "SPIRITUAL"} for l in i["labels"])]
    completed = [i for i in daily_goals if i["status_type"] == "completed"]
    all_done = len(daily_goals) > 0 and len(completed) == len(daily_goals)

    if all_done:
        state["streak"] = state.get("streak", 0) + 1
    elif now.hour >= 23:
        state["streak"] = 0
    state["last_streak_date"] = today_str
    save_streak(state)
    return state["streak"]


# ═══════════════════════════════════════════════════════════════════
# CACHE REFRESH
# ═══════════════════════════════════════════════════════════════════

async def refresh_cache():
    schedule_tasks = load_schedule()
    linear_issues = await fetch_issues()
    merged = merge_linear_status(schedule_tasks, linear_issues)
    calendar = await asyncio.to_thread(fetch_calendar_sync)
    state = load_streak()
    streak = compute_streak(state, linear_issues)
    cache["issues"] = merged
    cache["calendar"] = calendar
    cache["streak"] = streak
    cache["last_fetch"] = time.time()
    return cache


# ═══════════════════════════════════════════════════════════════════
# FASTAPI APP
# ═══════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"🔥 Dante Server v4 starting on port {PORT}")
    print(f"   Dashboard: http://localhost:{PORT}/")
    print(f"   API:       http://localhost:{PORT}/api/")
    await refresh_cache()
    yield

app = FastAPI(title="Dante Dashboard Server v4", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


# ── Endpoints ───────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the 7-day column SPA dashboard template."""
    if not DASHBOARD_TEMPLATE.exists():
        return HTMLResponse(
            "<h1>Dashboard template not found.</h1><p>Run import_schedule.py first.</p>",
            status_code=500)
    return HTMLResponse(DASHBOARD_TEMPLATE.read_text())


@app.get("/api/state")
async def api_state():
    """Return merged schedule + Linear data for the frontend."""
    if time.time() - cache["last_fetch"] > CACHE_TTL:
        await refresh_cache()
    return JSONResponse({
        "issues": cache["issues"],
        "calendar": cache["calendar"],
        "streak": cache["streak"],
        "cached_at": cache["last_fetch"],
    })


@app.post("/api/action")
async def api_action(data: dict):
    """Handle a task action (start/complete/skip/backlog) via Linear API."""
    issue_id = data.get("issue_id", "")
    action = data.get("action", "")

    state_map = {"start": "started", "complete": "completed", "skip": "canceled", "backlog": "backlog"}
    state_type = state_map.get(action)
    if not state_type:
        raise HTTPException(400, f"Unknown action: {action}")

    # Look up issue in Linear by matching the schedule task title
    if time.time() - cache["last_fetch"] > CACHE_TTL:
        await refresh_cache()

    # Find the task in our schedule to get its title, then find matching Linear issue
    schedule_task = None
    for t in cache["issues"]:
        if t["id"] == issue_id:
            schedule_task = t
            break

    if not schedule_task:
        raise HTTPException(404, f"Task {issue_id} not found in schedule")

    # Search Linear issues for title match AND day match
    linear_issues = await fetch_issues()
    tag_strip = _re.compile(
        r"\[(VOLATILE|TRADING|TRADING BOT|AI AVATAR|CREED CONSULT|NETWORKING|"
        r"DIGITAL PRODUCT|HEALTH|SPIRITUAL|ADMIN|PLANNING|MEALS/BREAKS|FREE TIME)\]"
    )
    day_re = _re.compile(r"\*\*[A-Z][a-z]+day, (\d{4}-\d{2}-\d{2})\*\*")

    issue_uuid = None
    task_day = schedule_task["day"]
    task_title = schedule_task["title"].lower()

    # First pass: try exact title + day match
    for li in linear_issues:
        clean = tag_strip.sub("", li["title"]).strip()
        if clean.lower() != task_title:
            continue
        # Check day in description
        desc = li.get("desc", "")
        day_match = day_re.search(desc)
        li_day = day_match.group(1) if day_match else None
        if li_day == task_day:
            issue_uuid = li["uuid"]
            break

    # Fallback: title-only match
    if not issue_uuid:
        for li in linear_issues:
            clean = tag_strip.sub("", li["title"]).strip()
            if clean.lower() == task_title:
                issue_uuid = li["uuid"]
                break

    if not issue_uuid:
        return {"success": False, "error": f"No Linear issue found for '{schedule_task['title']}'"}

    # Get workflow state ID
    wf_q = (
        'query { workflowStates(filter: { team: { id: { eq: "%s" } } }) '
        "{ nodes { id name type } } }" % TEAM_ID
    )
    wf_resp = await linear(wf_q)
    nodes = wf_resp.get("data", {}).get("workflowStates", {}).get("nodes", [])
    state_id = next((n["id"] for n in nodes if n["type"] == state_type), None)
    if not state_id:
        raise HTTPException(400, f"No workflow state for '{state_type}'")

    # Execute mutation
    mut = "mutation($id: String!, $input: IssueUpdateInput!) { issueUpdate(id: $id, input: $input) { success issue { identifier state { name type } } } }"
    result = await linear(mut, {"id": issue_uuid, "input": {"stateId": state_id}})
    data_out = result.get("data", {}).get("issueUpdate", {})
    if data_out.get("success"):
        # Refresh cache in background
        asyncio.create_task(refresh_cache())
    return data_out


@app.post("/api/refresh")
async def api_refresh():
    """Force cache refresh."""
    await refresh_cache()
    return {"success": True, "message": "Cache refreshed", "count": len(cache["issues"])}


@app.get("/api/ping")
async def api_ping():
    return {"status": "ok", "service": "dante-server-v4", "cached_at": cache["last_fetch"]}


# ═══════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")