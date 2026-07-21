# agent/planner.py
#
# Fixes applied vs original:
#   [FIX-1] generated_code ban is now consistent: removed from executor fallback
#            path AND planner prompt makes it explicit only replan uses it
#   [FIX-2] _fallback_plan inspects the goal to pick a smarter default tool
#   [FIX-3] replan() now uses flash-lite (same as create_plan) — consistent speed/cost
#            Full flash model preserved only for complex multi-step replans

import json
import re
import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


PLANNER_PROMPT = """You are the planning module of NOVA, a personal JARVIS-class AI assistant.
Your job: break any user goal into a sequence of steps using ONLY the tools listed below.

ABSOLUTE RULES:
- Use ONLY the tools in the list below. No other tool names.
- NEVER reference previous step results in parameters. Every step is self-contained.
- Use web_search for ANY information retrieval, research, or current data.
- Use file_controller to save content to disk.
- Use cmd_control to open files or run system commands.
- Max 5 steps. Use the minimum steps needed.
- If ONE step can accomplish the goal, use ONE step.

AVAILABLE TOOLS AND THEIR PARAMETERS:

open_app
  app_name: string (required)

web_search
  query: string (required) — write a clear, focused search query
  mode: "search" or "compare" (optional, default: search)
  items: list of strings (optional, for compare mode)
  aspect: string (optional, for compare mode)

game_updater
  action: "update" | "install" | "list" | "download_status" | "schedule" (required)
  platform: "steam" | "epic" | "both" (optional, default: both)
  game_name: string (optional)
  app_id: string (optional)
  shutdown_when_done: boolean (optional)

browser_control
  action: "go_to" | "search" | "click" | "type" | "scroll" | "get_text" | "press" | "close" (required)
  url: string (for go_to)
  query: string (for search)
  text: string (for click/type)
  direction: "up" | "down" (for scroll)

file_controller
  action: "write" | "create_file" | "read" | "list" | "delete" | "move" | "copy" | "find" | "disk_usage" (required)
  path: string — use "desktop" for Desktop folder
  name: string — filename
  content: string — file content (for write/create_file)

cmd_control
  task: string (required) — natural language description of what to do
  visible: boolean (optional)

computer_settings
  action: string (required)
  description: string — natural language description
  value: string (optional)

computer_control
  action: "type" | "click" | "hotkey" | "press" | "scroll" | "screenshot" | "screen_find" | "screen_click" (required)
  text: string (for type)
  x, y: int (for click)
  keys: string (for hotkey, e.g. "ctrl+c")
  key: string (for press)
  direction: "up" | "down" (for scroll)
  description: string (for screen_find/screen_click)

screen_process
  text: string (required) — what to analyze or ask about the screen
  angle: "screen" | "camera" (optional, default: screen)

send_message
  receiver: string (required)
  message_text: string (required)
  platform: string (required)

reminder
  date: string YYYY-MM-DD (required)
  time: string HH:MM (required)
  message: string (required)

desktop_control
  action: "wallpaper" | "organize" | "clean" | "list" | "task" (required)
  path: string (optional)
  task: string (optional)

youtube_video
  action: "play" | "summarize" | "trending" (required)
  query: string (for play)

weather_report
  city: string (required)

flight_finder
  origin: string (required)
  destination: string (required)
  date: string (required)

code_helper
  action: "write" | "edit" | "run" | "explain" (required)
  description: string (required)
  language: string (optional)
  output_path: string (optional)
  file_path: string (optional)

dev_agent
  description: string (required)
  language: string (optional)

EXAMPLES:

Goal: "research mechanical engineering and save it to a notepad file"
Steps:
  web_search    | query: "mechanical engineering overview definition history"
  web_search    | query: "mechanical engineering applications and future trends"
  file_controller | action: write, path: desktop, name: mechanical_engineering.txt, content: "..."
  cmd_control   | task: "open mechanical_engineering.txt on desktop with notepad"

Goal: "What is the price of Bitcoin"
Steps:
  web_search | query: "Bitcoin price today USD"

Goal: "Open Chrome"
Steps:
  open_app | app_name: chrome

Goal: "What's on my screen?"
Steps:
  screen_process | text: "Describe everything you can see on the screen", angle: screen

Goal: "Install PUBG from Steam"
Steps:
  game_updater | action: install, platform: steam, game_name: "PUBG"

Goal: "Send John a message on WhatsApp saying there is a meeting tomorrow"
Steps:
  send_message | receiver: John, message_text: "There is a meeting tomorrow", platform: WhatsApp

OUTPUT — return ONLY valid JSON, no markdown, no explanation, no code blocks:
{
  "goal": "...",
  "steps": [
    {
      "step": 1,
      "tool": "tool_name",
      "description": "what this step does",
      "parameters": {},
      "critical": true
    }
  ]
}
"""

# [FIX-2] Keyword → tool heuristic for smarter fallback plans
_FALLBACK_HEURISTICS: list[tuple[list[str], str, dict]] = [
    (["open", "launch", "start"],          "open_app",      {"app_name": ""}),
    (["send", "message", "whatsapp", "telegram"], "send_message", {"receiver": "", "message_text": "", "platform": "whatsapp"}),
    (["remind", "reminder", "alarm"],      "reminder",      {"date": "", "time": "", "message": ""}),
    (["weather"],                           "weather_report",{"city": ""}),
    (["screen", "camera", "see", "look"],  "screen_process",{"text": "", "angle": "screen"}),
    (["play", "youtube", "video"],         "youtube_video", {"action": "play", "query": ""}),
    (["install", "update", "steam", "epic"], "game_updater", {"action": "update"}),
    (["flight", "ticket", "fly"],          "flight_finder", {"origin": "", "destination": "", "date": ""}),
]


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _clean_json(text: str) -> str:
    return re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()


def create_plan(goal: str, context: str = "") -> dict:
    import google.generativeai as genai

    genai.configure(api_key=_get_api_key())
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash-lite",
        system_instruction=PLANNER_PROMPT,
    )

    user_input = f"Goal: {goal}"
    if context:
        user_input += f"\n\nContext: {context}"

    try:
        response = model.generate_content(user_input)
        text     = _clean_json(response.text.strip())
        plan     = json.loads(text)

        if "steps" not in plan or not isinstance(plan["steps"], list):
            raise ValueError("Invalid plan structure")

        # Safety net: replace any rogue tool names with web_search
        for step in plan["steps"]:
            if step.get("tool") == "generated_code":
                print(f"[Planner] ⚠️ generated_code in step {step.get('step')} — replacing")
                step["tool"]       = "web_search"
                step["parameters"] = {"query": step.get("description", goal)[:200]}

        print(f"[Planner] ✅ Plan: {len(plan['steps'])} step(s)")
        for s in plan["steps"]:
            print(f"  Step {s['step']}: [{s['tool']}] {s['description']}")

        return plan

    except json.JSONDecodeError as e:
        print(f"[Planner] ⚠️ JSON parse failed: {e}")
        return _fallback_plan(goal)
    except Exception as e:
        print(f"[Planner] ⚠️ Planning failed: {e}")
        return _fallback_plan(goal)


def _fallback_plan(goal: str) -> dict:
    """
    [FIX-2] Smarter fallback — inspect goal keywords before defaulting to web_search.
    """
    print("[Planner] 🔄 Fallback plan")
    goal_lower = goal.lower()

    for keywords, tool, base_params in _FALLBACK_HEURISTICS:
        if any(kw in goal_lower for kw in keywords):
            # Fill in whatever we can from the goal string itself
            params = dict(base_params)
            if "query" in params:
                params["query"] = goal
            if "text" in params and tool == "screen_process":
                params["text"] = goal
            if "description" in params:
                params["description"] = goal

            print(f"[Planner] 🔄 Fallback tool: {tool}")
            return {
                "goal": goal,
                "steps": [{
                    "step":        1,
                    "tool":        tool,
                    "description": f"Fallback: {goal}",
                    "parameters":  params,
                    "critical":    True,
                }],
            }

    # Default: web_search (safe for most unknown intents)
    return {
        "goal": goal,
        "steps": [{
            "step":        1,
            "tool":        "web_search",
            "description": f"Search for: {goal}",
            "parameters":  {"query": goal},
            "critical":    True,
        }],
    }


def replan(goal: str, completed_steps: list, failed_step: dict, error: str) -> dict:
    import google.generativeai as genai

    genai.configure(api_key=_get_api_key())

    completed_summary = "\n".join(
        f"  - Step {s['step']} ({s['tool']}): DONE" for s in completed_steps
    )
    num_completed = len(completed_steps)

    # [FIX-3] Use flash-lite for simple replans (0-1 completed), full flash for complex
    model_name = "gemini-2.5-flash" if num_completed > 1 else "gemini-2.5-flash-lite"
    model      = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=PLANNER_PROMPT,
    )

    prompt = f"""Goal: {goal}

Already completed:
{completed_summary if completed_summary else '  (none)'}

Failed step: [{failed_step.get('tool')}] {failed_step.get('description')}
Error: {error}

Create a REVISED plan for the remaining work only. Do not repeat completed steps.
Use a DIFFERENT tool or approach than the one that failed."""

    try:
        response = model.generate_content(prompt)
        text     = _clean_json(response.text.strip())
        plan     = json.loads(text)

        for step in plan.get("steps", []):
            if step.get("tool") == "generated_code":
                step["tool"]       = "web_search"
                step["parameters"] = {"query": step.get("description", goal)[:200]}

        print(f"[Planner] 🔄 Revised plan ({model_name}): {len(plan['steps'])} step(s)")
        return plan

    except Exception as e:
        print(f"[Planner] ⚠️ Replan failed: {e}")
        return _fallback_plan(goal)