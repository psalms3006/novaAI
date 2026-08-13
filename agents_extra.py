"""
agents_extra.py
════════════════
Orchestrator + specialist agent classes (Vision/Creative/Research/Code/Memory),
extracted from nova.py during Phase 2 refactor. Zero behavior change.

Imported by nova.py partway through its own top-to-bottom execution (at the
point this block used to live inline), so `import nova as _nova` below sees
a partially-initialized nova module that already has log / _execute_tool_sync
/ _task_desc / search_memory defined. Do not import this module standalone
before nova.py has reached that point.
"""
from __future__ import annotations
import os
import re
import time
import requests
from pathlib import Path
from typing import Any, Dict, List, Optional

from nova_agents import AgentType, AgentTask, BaseAgent  # canonical source

import nova as _nova
log = _nova.log
_execute_tool_sync = _nova._execute_tool_sync
_task_desc = _nova._task_desc
def _nova_get(name, default=None):
    try:
        return getattr(_nova, name, default)
    except Exception:
        return default


search_memory = _nova_get('search_memory')
get_all_memory_text = _nova_get('get_all_memory_text')
_vision_analyze = _nova_get('_vision_analyze')
_TOOL_AVAILABILITY = _nova_get('_TOOL_AVAILABILITY', {})
HAS_AUTONOMOUS_AGENTS = _nova_get('HAS_AUTONOMOUS_AGENTS', False)
get_all_agents = _nova_get('get_all_agents')


def _get_search_memory():
    if search_memory is not None:
        return search_memory
    try:
        from memory_extra import search_memory as _sm
        return _sm
    except Exception:
        return None


def _get_get_all_memory_text():
    if get_all_memory_text is not None:
        return get_all_memory_text
    try:
        from memory_extra import get_all_memory_text as _gm
        return _gm
    except Exception:
        return None

# ── ORCHESTRATOR ─────────────────────────────────────────────────────────────

ORCHESTRATOR_PROMPT = """You are the NOVA Orchestrator — the central intelligence that routes tasks to specialist agents."""


class OrchestratorAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentType.ORCHESTRATOR, ORCHESTRATOR_PROMPT)
        self.agents: Dict[AgentType, BaseAgent] = {}

    def register_agent(self, agent: Any) -> None:
        agent_type = getattr(agent, "agent_type", None)
        if agent_type is not None:
            self.agents[agent_type] = agent

    def route(self, user_request: str, meta: dict) -> Optional[str]:
        scores: Dict[AgentType, float] = {}
        req_lower = user_request.lower()

        keyword_map: Dict[AgentType, List[str]] = {
            AgentType.VISION: [
                "screen", "camera", "webcam", "photo", "image", "picture", "screenshot",
                "ocr", "read this", "what do you see", "describe", "face", "identify",
                "scan", "look at", "show me", "capture", "video", "frame",
            ],
            AgentType.CREATIVE: [
                "generate", "create", "make", "draw", "design", "edit", "modify",
                "logo", "banner", "thumbnail", "paint", "render", "illustration",
                "graphic", "visual", "animation",
            ],
            AgentType.RESEARCH: [
                "search", "find", "look up", "research", "what is", "who is", "when",
                "where", "why", "how to", "news", "price", "current", "latest",
                "information", "data", "facts", "weather", "stock", "market",
            ],
            AgentType.CODE: [
                "code", "script", "file", "folder", "directory", "write", "read",
                "edit", "fix", "bug", "program", "python", "run", "execute",
                "save", "delete", "move", "copy", "rename", "process",
            ],
            AgentType.MEMORY: [
                "remember", "recall", "what did", "previously", "before", "last time",
                "my name", "my preference", "what do you know", "what do you remember",
                "about me", "past", "history", "earlier",
            ],
            AgentType.BROWSER: [
                "go to", "visit", "browse", "check", "instagram", "twitter",
                "facebook", "linkedin", "website", "page", "post", "url", "link",
                "scroll", "navigate", "web", "site", "online", "profile",
            ],
            AgentType.MEETING: [
                "meeting", "zoom", "teams", "meet", "transcribe", "attend", "join",
                "record", "notes", "minutes", "conference", "call", "listen in",
                "take notes", "summarize meeting",
            ],
            AgentType.SURVEILLANCE: [
                "watch", "monitor", "surveillance", "capture", "detect",
                "audio", "listen", "alert", "notify", "track", "observe",
                "silent", "background", "continuous", "auto",
            ],
            AgentType.SPAWN: [
                "robot", "robotics", "iot", "esp32", "arduino", "raspberry", "jetson",
                "microcontroller", "edge device", "sensor", "motor", "actuator",
                "spawn", "create agent", "sub-agent", "deploy", "embedded",
                "autonomous robot", "smart device", "hardware",
            ],
        }

        for agent_type, keywords in keyword_map.items():
            scores[agent_type] = sum(1 for k in keywords if k in req_lower) / len(keywords) * 3

        if not scores:
            return None
        best_agent = max(scores.items(), key=lambda x: x[1])[0]
        best_score  = scores[best_agent]

        if best_score < 0.15:
            return None

        agent = self.agents.get(best_agent)

        # FIX: get_all_agents() returns Dict[AgentType, BaseAgent] — key with best_agent directly
        if agent is None and HAS_AUTONOMOUS_AGENTS:
            try:
                all_autonomous = get_all_agents()
                agent = all_autonomous.get(best_agent)  # type: ignore[arg-type]
            except Exception:
                pass

        if agent:
            # FIX: construct a proper AgentTask instead of passing a raw dict
            task_obj = AgentTask(
                task_id=str(int(time.time())),
                agent_type=best_agent,
                description=user_request,
                context={"user_request": user_request},
                result=None,
                status="pending",
            )
            return agent.execute(task_obj, meta)

        return None


# ── VISION AGENT ─────────────────────────────────────────────────────────────

class VisionAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentType.VISION, "NOVA Vision Agent")
        self.tools = {"vision", "computer_control"}

    def execute(self, task: Any, meta: dict) -> str:
        desc = _task_desc(task).lower()
        angle = "camera" if any(w in desc for w in ["camera", "webcam", "face", "me", "selfie"]) else "screen"
        if "ocr" in desc or "text" in desc:
            return _vision_analyze(angle=angle, ocr_only=True)
        if "face" in desc or "identify" in desc:
            return _vision_analyze(angle=angle, detect_faces=True)
        if "save" in desc and "photo" in desc:
            return _vision_analyze(angle="camera", save_reference=True)
        return _vision_analyze(angle=angle, question="Describe what you see in detail.")


# ── CREATIVE AGENT ───────────────────────────────────────────────────────────

class CreativeAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentType.CREATIVE, "NOVA Creative Agent")
        self.tools = {"file_processor", "computer_control"}
        self.replicate_api = os.getenv("REPLICATE_API_TOKEN")
        self.stability_api = os.getenv("STABILITY_API_KEY")
        self.openai_api    = os.getenv("OPENAI_API_KEY")

    def execute(self, task: Any, meta: dict) -> str:
        desc = _task_desc(task).lower()
        if any(w in desc for w in ["generate image", "create image", "make image", "draw", "generate a picture"]):
            return self._generate_image(_task_desc(task))
        if any(w in desc for w in ["edit image", "modify image", "change image", "filter"]):
            return "Creative Agent: Please provide the image path to edit."
        if any(w in desc for w in ["generate video", "create video", "make video", "animation"]):
            return self._generate_video(_task_desc(task))
        return "Creative Agent: I can generate images, edit photos, and create videos. What would you like me to make?"

    def _generate_image(self, prompt: str) -> str:
        if self.replicate_api:
            return self._generate_replicate(prompt)
        if self.stability_api:
            return self._generate_stability(prompt)
        if self.openai_api:
            return self._generate_openai(prompt)
        return (
            "Creative Agent: No image generation API configured. "
            "Set REPLICATE_API_TOKEN, STABILITY_API_KEY, or OPENAI_API_KEY in .env"
        )

    def _generate_replicate(self, prompt: str) -> str:
        try:
            import replicate  # type: ignore
            client = replicate.Client(api_token=self.replicate_api)
            output = client.run(
                "stability-ai/stable-diffusion:ac732df83cea7fff18b8472768c88ad041fa750ff7682a21f8186097f9e92d55",
                input={"prompt": prompt, "width": 1024, "height": 1024},
            )
            if output and len(output) > 0:
                img_url  = output[0] if isinstance(output, list) else output
                img_data = requests.get(img_url, timeout=30).content
                save_path = Path.home() / "Desktop" / f"nova_generated_{int(time.time())}.png"
                save_path.write_bytes(img_data)
                return f"Creative Agent: Generated image saved to {save_path}"
            return "Creative Agent: Image generation completed but no output received."
        except Exception as e:
            return f"Creative Agent: Replicate generation failed: {e}"

    def _generate_stability(self, prompt: str) -> str:
        try:
            response = requests.post(
                "https://api.stability.ai/v2beta/stable-image/generate/sd3",
                headers={"Authorization": f"Bearer {self.stability_api}", "Accept": "image/*"},
                files={"none": ("", "")},
                data={"prompt": prompt, "output_format": "png"},
                timeout=60,
            )
            if response.status_code == 200:
                save_path = Path.home() / "Desktop" / f"nova_generated_{int(time.time())}.png"
                save_path.write_bytes(response.content)
                return f"Creative Agent: Generated image saved to {save_path}"
            return f"Creative Agent: Stability API error: {response.status_code} — {response.text[:200]}"
        except Exception as e:
            return f"Creative Agent: Stability generation failed: {e}"

    def _generate_openai(self, prompt: str) -> str:
        try:
            r = requests.post(
                "https://api.openai.com/v1/images/generations",
                headers={"Authorization": f"Bearer {self.openai_api}", "Content-Type": "application/json"},
                json={"model": "dall-e-3", "prompt": prompt, "size": "1024x1024", "n": 1},
                timeout=60,
            )
            img_url  = r.json()["data"][0]["url"]
            img_data = requests.get(img_url, timeout=30).content
            save_path = Path.home() / "Desktop" / f"nova_generated_{int(time.time())}.png"
            save_path.write_bytes(img_data)
            return f"Creative Agent: Generated image saved to {save_path}"
        except Exception as e:
            return f"Creative Agent: OpenAI generation failed: {e}"

    def _generate_video(self, prompt: str) -> str:
        if self.replicate_api:
            try:
                import replicate  # type: ignore
                client = replicate.Client(api_token=self.replicate_api)
                output = client.run(
                    "stability-ai/stable-video-diffusion:3f0457e4619daac51203dedb472816fd4af51f3149fa7a9e0b5ffcf1b8172438",
                    input={"image": prompt, "frames": 14},
                )
                return f"Creative Agent: Video generation initiated. Output: {output}"
            except Exception as e:
                return f"Creative Agent: Video generation failed: {e}"
        return "Creative Agent: Video generation requires REPLICATE_API_TOKEN."


# ── RESEARCH AGENT ───────────────────────────────────────────────────────────

class ResearchAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentType.RESEARCH, "NOVA Research Agent")
        self.tools = {"web_search", "browser_control"}

    def execute(self, task: Any, meta: dict) -> str:
        desc = _task_desc(task)
        if _TOOL_AVAILABILITY.get("web_search"):
            try:
                result = _execute_tool_sync("web_search", {"query": desc}, meta)
                return f"Research Agent: {result}"
            except Exception as e:
                return f"Research Agent: Search failed: {e}"
        return "Research Agent: web_search tool not available."


# ── CODE AGENT ───────────────────────────────────────────────────────────────

class CodeAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentType.CODE, "NOVA Code Agent")
        self.tools = {"self_editor", "file_controller", "file_processor", "computer_control"}

    def execute(self, task: Any, meta: dict) -> str:
        desc     = _task_desc(task)
        desc_low = desc.lower()

        if any(w in desc_low for w in ["edit your code", "fix yourself", "update your"]):
            return _execute_tool_sync("self_editor", {"action": "read"}, meta)

        if any(w in desc_low for w in ["read file", "open file", "show file", "file content"]):
            # Fixed regex: proper character class for path matching
            path_match = re.search(r"[\w\-./\\]+\.[\w]+", desc)
            if path_match:
                return _execute_tool_sync(
                    "file_processor",
                    {"file_path": path_match.group(), "action": "read"},
                    meta,
                )

        if any(w in desc_low for w in ["write file", "create file", "save file"]):
            return "Code Agent: Please specify the file path and content to write."

        return "Code Agent: I can read, edit, and execute code. What file or operation do you need?"


# ── MEMORY AGENT ─────────────────────────────────────────────────────────────

class MemoryAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(AgentType.MEMORY, "NOVA Memory Agent")
        self.tools = {"remember_fact"}

    def execute(self, task: Any, meta: dict) -> str:
        desc     = _task_desc(task)
        desc_low = desc.lower()

        if any(w in desc_low for w in ["what do you remember", "what do you know", "about me", "my info"]):
            return get_all_memory_text(meta)

        results = search_memory(desc, top_k=5)
        if results:
            return "Memory Agent: Here's what I found:\n" + "\n".join(f"• {r}" for r in results)

        return "Memory Agent: I don't have specific memories about that yet."


# ── AGENT REGISTRY ────────────────────────────────────────────────────────────

_orchestrator: Optional[OrchestratorAgent] = None


def init_agents() -> OrchestratorAgent:
    global _orchestrator
    _orchestrator = OrchestratorAgent()
    _orchestrator.register_agent(VisionAgent())
    _orchestrator.register_agent(CreativeAgent())
    _orchestrator.register_agent(ResearchAgent())
    _orchestrator.register_agent(CodeAgent())
    _orchestrator.register_agent(MemoryAgent())

    # v3.4: Register autonomous agents if available
    try:
        from nova_agents import (  # noqa: F401
            BrowserAgent as _BA,
            MeetingAgent as _MA,
            SurveillanceAgent as _SA,
            SpawnAgent as _SPA,
        )
        for agent_cls in [_BA, _MA, _SA, _SPA]:
            try:
                agent = agent_cls()
                if isinstance(agent, BaseAgent):
                    _orchestrator.register_agent(agent)
                else:
                    log.warning(
                        f"{agent_cls.__name__} does not inherit from BaseAgent, skipping registration"
                    )
            except Exception as e:
                log.warning(f"Failed to register {agent_cls.__name__}: {e}")
        log.info("Agent system initialized — autonomous agents registered (v3.4)")
    except Exception as e:
        log.warning(f"Autonomous agent registration failed: {e}")
        log.info("Agent system initialized — 5 specialist agents registered (autonomous agents not available)")

    return _orchestrator


def agent_process(user_request: str, meta: dict) -> Optional[str]:
    global _orchestrator
    if _orchestrator is None:
        init_agents()
    if _orchestrator is None:
        return None
    try:
        _cmd = user_request.strip()
        if _cmd in ('/goal', '/goals'):
            try:
                from core.goal_engine import GoalEngine
                engine = GoalEngine()
                goals = engine.recover_unfinished()
                if not goals:
                    return 'No active goals.'
                return '\n'.join([f"• {g.goal_id}: {g.mission} [{g.status}]" for g in goals])
            except Exception as _goal_err:
                return f'Goal lookup failed: {_goal_err}'
        if _cmd.startswith('/goal create '):
            _mission = _cmd.split(maxsplit=2)[2]
            try:
                from core.goal_engine import GoalEngine
                engine = GoalEngine()
                goal = engine.create_goal(_mission)
                return f'Goal created: {goal.goal_id}'
            except Exception as _create_err:
                return f'Goal creation failed: {_create_err}'
        return _orchestrator.route(user_request, meta)
    except Exception as e:
        log.warning(f"Agent routing failed: {e}. Falling back to general processing.")
        return None

