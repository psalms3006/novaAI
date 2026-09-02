# NOVA Core Architecture Files



============================================================
FILE: core\capability_bus.py
============================================================

```python
"""
Capability bus — thin abstraction over NOVA's existing tool registry/dispatch.

This does not replace `_execute_tool_sync`; it adds:
- normalized capability metadata
- lookup by step description/agent
- risk/confirmation hints for the safety gate
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

log = logging.getLogger("nova.capabilities")

_CAPABILITIES = [
    {
        "name": "web_search",
        "description": "search the web for information",
        "agent": "research",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": False,
        "verification_hint": "returned sources/links or summary",
    },
    {
        "name": "open_app",
        "description": "open a desktop application",
        "agent": "browser",
        "risk_level": "medium",
        "requires_confirmation": True,
        "reversible": False,
        "verification_hint": "app launched or error returned",
    },
    {
        "name": "file_processor",
        "description": "read, summarize, or extract text from files",
        "agent": "code",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": False,
        "verification_hint": "file content snippet or summary returned",
    },
    {
        "name": "self_editor",
        "description": "edit project files safely with backup",
        "agent": "code",
        "risk_level": "medium",
        "requires_confirmation": True,
        "reversible": True,
        "verification_hint": "patch applied or diff shown",
    },
    {
        "name": "computer_settings",
        "description": "read or change system settings",
        "agent": "browser",
        "risk_level": "high",
        "requires_confirmation": True,
        "reversible": True,
        "verification_hint": "setting changed or current value reported",
    },
    {
        "name": "browser_control",
        "description": "control browser navigation and interaction",
        "agent": "browser",
        "risk_level": "medium",
        "requires_confirmation": False,
        "reversible": False,
        "verification_hint": "page snapshot or action confirmation",
    },
    {
        "name": "computer_control",
        "description": "direct mouse/keyboard control",
        "agent": "browser",
        "risk_level": "high",
        "requires_confirmation": True,
        "reversible": False,
        "verification_hint": "screenshot before/after or action confirmation",
    },
    {
        "name": "vision",
        "description": "capture or analyze screen/camera input",
        "agent": "vision",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": False,
        "verification_hint": "image analysis result or saved path",
    },
    {
        "name": "planner",
        "description": "create or inspect task plans/reminders",
        "agent": "orchestrator",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": True,
        "verification_hint": "planner task list updated",
    },
    {
        "name": "remember_fact",
        "description": "store a memory fact for later retrieval",
        "agent": "orchestrator",
        "risk_level": "low",
        "requires_confirmation": False,
        "reversible": True,
        "verification_hint": "fact count increased or confirmation returned",
    },
]


@dataclass
class Capability:
    name: str
    description: str
    agent: str
    risk_level: str
    requires_confirmation: bool
    reversible: bool
    verification_hint: str

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Capability":
        return cls(**data)


class CapabilityBus:
    """Lookup layer between goal steps and NOVA tool dispatch."""

    def __init__(self, capabilities: Optional[list] = None) -> None:
        self._by_name: Dict[str, Capability] = {}
        self._by_agent: Dict[str, list] = {}
        for raw in capabilities or _CAPABILITIES:
            cap = Capability.from_dict(raw)
            self._by_name[cap.name] = cap
            self._by_agent.setdefault(cap.agent, []).append(cap)

    def get(self, name: str) -> Optional[Capability]:
        return self._by_name.get(name)

    def find_by_agent(self, agent: str) -> list:
        return list(self._by_agent.get(agent, []))

    def find_for_step(self, description: str, agent: Optional[str] = None) -> Optional[Capability]:
        text = description.lower()
        candidates = []
        for cap in self._by_name.values():
            if cap.name in text or any(kw in text for kw in cap.description.split()):
                candidates.append(cap)
        if agent:
            candidates = [c for c in candidates if c.agent == agent] + [c for c in candidates if c.agent != agent]
        return candidates[0] if candidates else None

    def all_tool_names(self) -> list:
        return list(self._by_name.keys())
```


============================================================
FILE: core\execution_engine.py
============================================================

```python
"""
Execution Engine — runs GoalSteps through NOVA's existing tool/agent dispatch.

Additive. Wraps existing tool/agent functions so goals can drive execution.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from core.goal_engine import GoalEngine, GoalStep
from core.capability_bus import CapabilityBus, Capability

log = logging.getLogger("nova.execution")


class StepOutcome:
    SUCCESS = "SUCCESS"
    FAILURE = "FAILURE"
    BLOCKED = "BLOCKED"
    NEEDS_INPUT = "NEEDS_INPUT"


@dataclass
class StepResult:
    step_id: str
    status: str
    output: Optional[str]
    verification: Optional[str]
    outcome: str
    retry_count: int = 0
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "step_id": self.step_id,
            "status": self.status,
            "output": self.output,
            "verification": self.verification,
            "outcome": self.outcome,
            "retry_count": self.retry_count,
            "error": self.error,
            "metadata": self.metadata,
        }


class ExecutionEngine:
    """
    Execute one goal step using NOVA's existing runtime.

    Parameters
    ----------
    goal_engine: GoalEngine
    capability_bus: CapabilityBus
    tool_fn: callable(tool_name, args, meta) -> str
    agent_fn: optional callable(agent, message, meta) -> str
    """

    def __init__(
        self,
        goal_engine: GoalEngine,
        capability_bus: Optional[CapabilityBus] = None,
        tool_fn: Optional[Callable[[str, Dict[str, Any], Dict[str, Any]], str]] = None,
        agent_fn: Optional[Callable[[str, str, Dict[str, Any]], str]] = None,
    ) -> None:
        self.goals = goal_engine
        self.bus = capability_bus or CapabilityBus()
        self.tool_fn = tool_fn
        self.agent_fn = agent_fn

    def execute_next_step(self, goal_id: str, meta: Optional[Dict[str, Any]] = None) -> Optional[StepResult]:
        goal = self.goals.store.get_goal(goal_id)
        if not goal:
            return None

        step = self.goals.get_next_step(goal_id)
        if not step:
            return None

        meta = meta or {}
        capability = self.bus.get(step.tool or "") if step.tool else None
        if capability is None:
            capability = self.bus.find_for_step(step.description, step.agent)

        try:
            if step.agent and self.agent_fn:
                raw_output = self.agent_fn(step.agent, step.description, meta)
            elif step.tool and self.tool_fn:
                raw_output = self.tool_fn(step.tool, step.input or {}, meta)
            else:
                raw_output = f"[no-op] {step.description}"
        except Exception as e:
            log.error("Step execution failed: %s", e)
            self.goals.fail_step(goal_id, step.step_id, str(e), retry=True)
            return StepResult(
                step_id=step.step_id,
                status="FAILED",
                output=None,
                verification=None,
                outcome=StepOutcome.FAILURE,
                error=str(e),
            )

        verification = self._verify(step, raw_output, capability)
        outcome = StepOutcome.SUCCESS if getattr(verification, "status", "") == "CONFIRMED_SUCCESS" else StepOutcome.BLOCKED

        step_status = step.status
        if outcome == StepOutcome.SUCCESS:
            updated = self.goals.complete_step(
                goal_id,
                step.step_id,
                output=raw_output,
                verification=getattr(verification, "reason", None),
                outcome=outcome,
            )
            if updated:
                plan_map = {s.step_id: s for s in updated.plan}
                step_status = plan_map.get(step.step_id, step).status
        else:
            self.goals.fail_step(goal_id, step.step_id, "verification did not confirm success", retry=True)
            step_status = "FAILED"

        self._store_outcome_memory(goal, step, outcome, raw_output)

        return StepResult(
            step_id=step.step_id,
            status=step_status,
            output=raw_output,
            verification=getattr(verification, "reason", None),
            outcome=outcome,
            metadata={"capability": capability.name if capability else None},
        )

    def _verify(self, step: GoalStep, raw_output: str, capability: Optional[Capability]) -> Any:
        from core.verification_engine import VerificationEngine
        verifier = VerificationEngine()
        return verifier.verify(step, raw_output, capability)

    def _store_outcome_memory(self, goal: Goal, step: GoalStep, outcome: str, raw_output: Optional[str]) -> None:
        try:
            add_memory_fact_fn = None
            try:
                from memory_extra import add_memory_fact as _add
                add_memory_fact_fn = _add
            except Exception:
                try:
                    import nova as _nova
                    add_memory_fact_fn = getattr(_nova, 'add_memory_fact', None)
                except Exception:
                    pass
            if add_memory_fact_fn is None:
                return
            meta = {
                user_name: goal.metadata.get(user_name, ),
                goal_id: goal.goal_id,
                step_id: step.step_id,
                agent: step.agent,
                tool: step.tool,
            }
            text = f"Outcome: {goal.mission} | step {step.step_id}: {outcome}"
            if raw_output:
                text += f" | {raw_output[:140]}"
            add_memory_fact_fn(text, meta)
        except Exception as memory_err:
            log.warning("Outcome memory store failed: %s", memory_err)


@dataclass
class GoalProgress:
    goal_id: str
    mission: str
    status: str
    completed_steps: int
    total_steps: int
    next_step: Optional[str]
    last_outcome: Optional[str]
    last_output: Optional[str]

    @classmethod
    def from_goal(cls, goal: Any) -> "GoalProgress":
        plan = getattr(goal, "plan", [])
        completed = sum(1 for s in plan if getattr(s, "status", "") == "COMPLETED")
        next_step = None
        for s in plan:
            if getattr(s, "status", "") == "PENDING":
                next_step = getattr(s, "step_id", None)
                break
        return cls(
            goal_id=getattr(goal, "goal_id", ""),
            mission=getattr(goal, "mission", ""),
            status=getattr(goal, "status", ""),
            completed_steps=completed,
            total_steps=len(plan),
            next_step=next_step,
            last_outcome=plan[-1].outcome if plan else None,
            last_output=plan[-1].output if plan else None,
        )
```


============================================================
FILE: agent\planner.py
============================================================

```python
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
```


============================================================
FILE: agent\executor.py
============================================================

```python
# agent/executor.py
#
# Fixes applied vs original:
#   [FIX-1] screen_process result is now used — not replaced with hardcoded string
#   [FIX-2] Unknown tool no longer silently falls back to banned generated_code;
#            raises ValueError so planner can replan with a real tool
#   [FIX-3] _inject_context extended to code_helper + dev_agent content passing
#   [FIX-4] retry loop respects max_retries from error_handler, not hardcoded 3
#   [FIX-5] _translate_to_goal_language is opt-in via NOVA_AUTO_TRANSLATE env var

import json
import os
import re
import sys
import threading
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from agent.planner       import create_plan, replan
from agent.error_handler import analyze_error, generate_fix, ErrorDecision


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"


def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


# ─── Generated code runner (internal fallback only, never planner-visible) ────

def _run_generated_code(description: str, speak: Callable | None = None) -> str:
    import google.generativeai as genai

    if speak:
        speak("Writing custom code for this task, sir.")

    home      = Path.home()
    desktop   = home / "Desktop"
    downloads = home / "Downloads"
    documents = home / "Documents"

    if not desktop.exists():
        try:
            import winreg
            key     = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
            )
            desktop = Path(winreg.QueryValueEx(key, "Desktop")[0])
        except Exception:
            pass

    genai.configure(api_key=_get_api_key())
    model = genai.GenerativeModel(
        model_name="gemini-2.5-flash",
        system_instruction=(
            "You are an expert Python developer. "
            "Write clean, complete, working Python code. "
            "Use standard library + common packages. "
            "Install missing packages with subprocess + pip if needed. "
            "Return ONLY the Python code. No explanation, no markdown, no backticks.\n\n"
            f"SYSTEM PATHS:\n"
            f"  Desktop   = r'{desktop}'\n"
            f"  Downloads = r'{downloads}'\n"
            f"  Documents = r'{documents}'\n"
            f"  Home      = r'{home}'\n"
        ),
    )

    try:
        response = model.generate_content(
            f"Write Python code to accomplish this task:\n\n{description}"
        )
        code = response.text.strip()
        code = re.sub(r"```(?:python)?", "", code).strip().rstrip("`").strip()

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f:
            f.write(code)
            tmp_path = f.name

        print(f"[Executor] 🐍 Running generated code: {tmp_path}")

        result = subprocess.run(
            [sys.executable, tmp_path],
            capture_output=True, text=True,
            timeout=120, cwd=str(Path.home()),
        )

        try:
            os.unlink(tmp_path)
        except Exception:
            pass

        output = result.stdout.strip()
        error  = result.stderr.strip()

        if result.returncode == 0 and output:
            return output
        elif result.returncode == 0:
            return "Task completed successfully."
        elif error:
            raise RuntimeError(f"Code error: {error[:400]}")
        return "Completed."

    except subprocess.TimeoutExpired:
        raise RuntimeError("Generated code timed out after 120 seconds.")
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Generated code failed: {e}")


# ─── Language detection / translation (opt-in) ────────────────────────────────

# [FIX-5] Only translate when explicitly enabled
_AUTO_TRANSLATE = os.environ.get("NOVA_AUTO_TRANSLATE", "0").strip() == "1"


def _detect_language(text: str) -> str:
    import google.generativeai as genai
    genai.configure(api_key=_get_api_key())
    model = genai.GenerativeModel("gemini-2.5-flash-lite")
    try:
        response = model.generate_content(
            f"What language is this text written in? "
            f"Reply with ONLY the language name in English (e.g. Turkish, English, French).\n\n"
            f"Text: {text[:200]}"
        )
        return response.text.strip()
    except Exception:
        return "English"


def _translate_to_goal_language(content: str, goal: str) -> str:
    if not goal or not _AUTO_TRANSLATE:
        return content
    try:
        import google.generativeai as genai
        genai.configure(api_key=_get_api_key())
        model = genai.GenerativeModel("gemini-2.5-flash")

        target_lang = _detect_language(goal)
        if target_lang.lower() == "english":
            return content

        print(f"[Executor] 🌐 Translating to: {target_lang}")
        prompt = (
            f"You are a professional translator. "
            f"Translate the following text into {target_lang}.\n"
            f"IMPORTANT:\n"
            f"- Translate EVERYTHING, leave nothing in English\n"
            f"- Keep all facts, numbers, and data intact\n"
            f"- Keep the structure and formatting\n"
            f"- Output ONLY the translated text, nothing else\n\n"
            f"Text to translate:\n{content[:4000]}"
        )
        response    = model.generate_content(prompt)
        translated  = response.text.strip()
        print(f"[Executor] ✅ Translation done ({target_lang})")
        return translated
    except Exception as e:
        print(f"[Executor] ⚠️ Translation failed: {e}")
        return content


# ─── Context injection ────────────────────────────────────────────────────────

def _inject_context(params: dict, tool: str, step_results: dict, goal: str = "") -> dict:
    """
    [FIX-3] Extended to pass prior results into code_helper and dev_agent,
    not just file_controller. Uses a shared helper to avoid repetition.
    """
    if not step_results:
        return params

    params = dict(params)

    def _best_prior() -> str:
        candidates = [
            v for v in step_results.values()
            if v and len(v) > 100 and v not in ("Done.", "Completed.")
        ]
        if not candidates:
            return ""
        combined    = "\n\n---\n\n".join(candidates)
        return _translate_to_goal_language(combined, goal)

    if tool == "file_controller" and params.get("action") in ("write", "create_file"):
        if not params.get("content") or len(params.get("content", "")) < 50:
            best = _best_prior()
            if best:
                params["content"] = best
                print("[Executor] 💉 Injected content into file_controller")

    elif tool in ("code_helper", "dev_agent"):
        # Append prior research/results to the description so the agent has context
        prior = _best_prior()
        if prior:
            existing_desc = params.get("description", "")
            params["description"] = f"{existing_desc}\n\nContext from prior steps:\n{prior[:2000]}"
            print(f"[Executor] 💉 Injected context into {tool}")

    return params


# ─── Tool dispatch ────────────────────────────────────────────────────────────

def _call_tool(tool: str, parameters: dict, speak: Callable | None) -> str:

    if tool == "open_app":
        from actions.open_app import open_app
        return open_app(parameters=parameters, player=None) or "Done."

    elif tool == "web_search":
        from actions.web_search import web_search
        return web_search(parameters=parameters, player=None) or "Done."

    elif tool == "game_updater":
        from actions.game_updater import game_updater
        return game_updater(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "browser_control":
        from actions.browser_control import browser_control
        return browser_control(parameters=parameters, player=None) or "Done."

    elif tool == "file_controller":
        from actions.file_controller import file_controller
        return file_controller(parameters=parameters, player=None) or "Done."

    elif tool == "cmd_control":
        from actions.cmd_control import cmd_control
        return cmd_control(parameters=parameters, player=None) or "Done."

    elif tool == "code_helper":
        from actions.code_helper import code_helper
        return code_helper(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "dev_agent":
        from actions.dev_agent import dev_agent
        return dev_agent(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "screen_process":
        from actions.screen_processor import screen_process
        # [FIX-1] Use the actual return value — screen_process now returns str
        result = screen_process(parameters=parameters, player=None)
        return result or "Screen captured and analyzed."

    elif tool == "send_message":
        from actions.send_message import send_message
        return send_message(parameters=parameters, player=None) or "Done."

    elif tool == "reminder":
        from actions.reminder import reminder
        return reminder(parameters=parameters, player=None) or "Done."

    elif tool == "youtube_video":
        from actions.youtube_video import youtube_video
        return youtube_video(parameters=parameters, player=None) or "Done."

    elif tool == "weather_report":
        from actions.weather_report import weather_action
        return weather_action(parameters=parameters, player=None) or "Done."

    elif tool == "computer_settings":
        from actions.computer_settings import computer_settings
        return computer_settings(parameters=parameters, player=None) or "Done."

    elif tool == "desktop_control":
        from actions.desktop import desktop_control
        return desktop_control(parameters=parameters, player=None) or "Done."

    elif tool == "computer_control":
        from actions.computer_control import computer_control
        return computer_control(parameters=parameters, player=None) or "Done."

    elif tool == "flight_finder":
        from actions.flight_finder import flight_finder
        return flight_finder(parameters=parameters, player=None, speak=speak) or "Done."

    elif tool == "generated_code":
        # generated_code is an internal-only tool the planner never emits.
        # It reaches here only via generate_fix() in error recovery.
        description = parameters.get("description", "")
        if not description:
            raise ValueError("generated_code requires a 'description' parameter.")
        return _run_generated_code(description, speak=speak)

    else:
        # [FIX-2] Do NOT silently fall back to generated_code.
        # Raise so the executor's error handler can trigger a proper replan
        # with a tool the planner actually knows about.
        raise ValueError(
            f"Unknown tool '{tool}'. "
            "Add it to executor._call_tool or fix the plan to use a known tool."
        )


# ─── Main executor ────────────────────────────────────────────────────────────

class AgentExecutor:

    MAX_REPLAN_ATTEMPTS = 2

    def execute(
        self,
        goal:        str,
        speak:       Callable | None        = None,
        cancel_flag: threading.Event | None = None,
    ) -> str:
        print(f"\n[Executor] 🎯 Goal: {goal}")

        replan_attempts = 0
        completed_steps = []
        step_results    = {}
        plan            = create_plan(goal)

        while True:
            steps = plan.get("steps", [])

            if not steps:
                msg = "I couldn't create a valid plan for this task, sir."
                if speak:
                    speak(msg)
                return msg

            success      = True
            failed_step  = None
            failed_error = ""

            for step in steps:
                if cancel_flag and cancel_flag.is_set():
                    if speak:
                        speak("Task cancelled, sir.")
                    return "Task cancelled."

                step_num = step.get("step", "?")
                tool     = step.get("tool", "generated_code")
                desc     = step.get("description", "")
                params   = step.get("parameters", {})

                params = _inject_context(params, tool, step_results, goal=goal)

                print(f"\n[Executor] ▶️ Step {step_num}: [{tool}] {desc}")

                # [FIX-4] max_retries comes from error_handler analysis, not hardcoded
                max_attempts = 3   # default; overridden on first failure
                attempt      = 1
                step_ok      = False

                while attempt <= max_attempts:
                    if cancel_flag and cancel_flag.is_set():
                        break
                    try:
                        result             = _call_tool(tool, params, speak)
                        step_results[step_num] = result
                        completed_steps.append(step)
                        print(f"[Executor] ✅ Step {step_num} done: {str(result)[:120]}")
                        step_ok = True
                        break

                    except Exception as e:
                        error_msg = str(e)
                        print(f"[Executor] ❌ Step {step_num} attempt {attempt} failed: {error_msg}")

                        recovery    = analyze_error(step, error_msg, attempt=attempt)
                        decision    = recovery["decision"]
                        user_msg    = recovery.get("user_message", "")
                        # [FIX-4] respect max_retries from the LLM analysis
                        max_attempts = max(attempt + 1, recovery.get("max_retries", 1) + 1)

                        if speak and user_msg:
                            speak(user_msg)

                        if decision == ErrorDecision.RETRY:
                            attempt += 1
                            import time
                            time.sleep(2)
                            continue

                        elif decision == ErrorDecision.SKIP:
                            print(f"[Executor] ⏭️ Skipping step {step_num}")
                            completed_steps.append(step)
                            step_ok = True
                            break

                        elif decision == ErrorDecision.ABORT:
                            msg = f"Task aborted, sir. {recovery.get('reason', '')}"
                            if speak:
                                speak(msg)
                            return msg

                        else:  # REPLAN
                            fix_suggestion = recovery.get("fix_suggestion", "")
                            if fix_suggestion and tool != "generated_code":
                                try:
                                    fixed_step = generate_fix(step, error_msg, fix_suggestion)
                                    if speak:
                                        speak("Trying an alternative approach, sir.")
                                    res = _call_tool(
                                        fixed_step["tool"],
                                        fixed_step["parameters"],
                                        speak,
                                    )
                                    step_results[step_num] = res
                                    completed_steps.append(step)
                                    step_ok = True
                                    break
                                except Exception as fix_err:
                                    print(f"[Executor] ⚠️ Fix failed: {fix_err}")

                            failed_step  = step
                            failed_error = error_msg
                            success      = False
                            break

                if not step_ok and not failed_step:
                    failed_step  = step
                    failed_error = "Max retries exceeded"
                    success      = False

                if not success:
                    break

            if success:
                return self._summarize(goal, completed_steps, speak)

            if replan_attempts >= self.MAX_REPLAN_ATTEMPTS:
                msg = f"Task failed after {replan_attempts} replan attempts, sir."
                if speak:
                    speak(msg)
                return msg

            if speak:
                speak("Adjusting my approach, sir.")

            replan_attempts += 1
            plan = replan(goal, completed_steps, failed_step, failed_error)

    def _summarize(self, goal: str, completed_steps: list, speak: Callable | None) -> str:
        fallback = f"All done, sir. Completed {len(completed_steps)} steps for: {goal[:60]}."
        try:
            import google.generativeai as genai
            genai.configure(api_key=_get_api_key())
            model     = genai.GenerativeModel(model_name="gemini-2.5-flash-lite")
            steps_str = "\n".join(f"- {s.get('description', '')}" for s in completed_steps)
            prompt    = (
                f'User goal: "{goal}"\n'
                f"Completed steps:\n{steps_str}\n\n"
                "Write a single natural sentence summarising what was accomplished. "
                "Address the user as 'sir'. Be direct and positive."
            )
            response = model.generate_content(prompt)
            summary  = response.text.strip()
            if speak:
                speak(summary)
            return summary
        except Exception:
            if speak:
                speak(fallback)
            return fallback
```


============================================================
FILE: agent\tools\registry.py
============================================================

```python
"""Tool registry — extend by registering self-contained tools, not editing the brain loop."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable

ToolHandler = Callable[[dict[str, Any], "ToolContext"], str]


@dataclass
class ToolContext:
    """Mutable per-session state tools may read or write."""

    session_facts: list[str] = field(default_factory=list)


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: ToolHandler
    requires_confirmation: bool = False


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def schemas(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.parameters,
            }
            for spec in self._tools.values()
        ]

    def run(self, name: str, arguments: dict[str, Any], ctx: ToolContext) -> str:
        spec = self._tools.get(name)
        if spec is None:
            return f"Unknown tool '{name}'. Available: {', '.join(self._tools)}."

        try:
            return spec.handler(arguments, ctx)
        except Exception as exc:
            return f"Tool '{name}' failed: {exc}"


def _web_search_handler(args: dict[str, Any], _ctx: ToolContext) -> str:
    if os.getenv("AGENT_MOCK_FAIL_WEB_SEARCH", "").strip().lower() in ("1", "true", "yes"):
        raise RuntimeError("Mock search backend unavailable (AGENT_MOCK_FAIL_WEB_SEARCH).")

    query = str(args.get("query", "")).strip()
    if not query:
        return "Error: query is required."

    from actions.web_search import web_search

    return web_search({"query": query})


def _open_app_handler(args: dict[str, Any], _ctx: ToolContext) -> str:
    app_name = str(args.get("app_name", "")).strip()
    if not app_name:
        return "Error: app_name is required."

    from actions.open_app import execute

    return execute({"app_name": app_name})


def _remember_fact_handler(args: dict[str, Any], ctx: ToolContext) -> str:
    fact = str(args.get("fact", "")).strip()
    if not fact:
        return "Error: fact is required."

    ctx.session_facts.append(fact)
    return f"Remembered for this session: {fact}"


def _list_session_facts_handler(_args: dict[str, Any], ctx: ToolContext) -> str:
    if not ctx.session_facts:
        return "No facts remembered this session yet."
    lines = [f"{i}. {fact}" for i, fact in enumerate(ctx.session_facts, 1)]
    return "Session facts:\n" + "\n".join(lines)


def _mock_web_search_handler(args: dict[str, Any], _ctx: ToolContext) -> str:
    if os.getenv("AGENT_MOCK_FAIL_WEB_SEARCH", "").strip().lower() in ("1", "true", "yes"):
        raise RuntimeError("Mock search backend unavailable.")

    query = str(args.get("query", "")).strip()
    canned = {
        "capital of france": "Paris is the capital of France.",
        "python": "Python is a popular programming language.",
    }
    key = query.lower()
    for fragment, answer in canned.items():
        if fragment in key:
            return answer
    return f"[mock search] Top result for '{query}': sample answer with relevant details."


def _mock_open_app_handler(args: dict[str, Any], _ctx: ToolContext) -> str:
    app_name = str(args.get("app_name", "")).strip()
    return f"[mock] Opened {app_name}."


def build_default_registry(*, mock: bool = False) -> ToolRegistry:
    registry = ToolRegistry()

    search_handler = _mock_web_search_handler if mock else _web_search_handler
    open_handler = _mock_open_app_handler if mock else _open_app_handler

    registry.register(
        ToolSpec(
            name="web_search",
            description=(
                "Search the web for current information, facts, news, or anything "
                "that needs an internet lookup. Use when the user asks about recent "
                "events or facts you are not sure of."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query — be specific.",
                    }
                },
                "required": ["query"],
            },
            handler=search_handler,
            requires_confirmation=False,
        )
    )

    registry.register(
        ToolSpec(
            name="open_app",
            description=(
                "Open or launch an application on this Windows computer. Use when "
                "the user asks to open, start, or launch a program (Chrome, Notepad, "
                "VS Code, Spotify, etc.)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Application name, e.g. 'Notepad', 'Chrome', 'VS Code'.",
                    }
                },
                "required": ["app_name"],
            },
            handler=open_handler,
            requires_confirmation=False,
        )
    )

    registry.register(
        ToolSpec(
            name="remember_fact",
            description=(
                "Store a fact about the user for this session (preferences, name, "
                "habits). Use when they say 'remember that…' or ask you to note something."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "fact": {
                        "type": "string",
                        "description": "One clear statement to remember, e.g. 'Prefers morning meetings'.",
                    }
                },
                "required": ["fact"],
            },
            handler=_remember_fact_handler,
            requires_confirmation=False,
        )
    )

    registry.register(
        ToolSpec(
            name="list_session_facts",
            description=(
                "List facts remembered about the user during this session. Use when "
                "they ask what you remember or what's on their list of notes."
            ),
            parameters={"type": "object", "properties": {}},
            handler=_list_session_facts_handler,
            requires_confirmation=False,
        )
    )

    return registry
```
