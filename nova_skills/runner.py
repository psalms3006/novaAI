"""nova_skills.runner — doing what a capability says, and proving it worked.

A workflow is a list of steps, each either one of NOVA's tools or a call to a
provider:

    {"tool": "web_search", "args": {"query": "{topic} reviews"}}
    {"provider": "img-api", "method": "POST", "path": "/v1/images",
     "body": {"prompt": "{prompt}"}}

`{name}` placeholders are filled from the inputs and from earlier steps'
outputs (`{step1}`, `{step2}`, ...). Each step's result is checked; a step that
reports failure stops the run -- the result says which step and why, and the
run is never reported as done.

`verify` runs the capability's declared test and records the outcome. That is
the only path by which a capability becomes learned.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from nova_core.errors import ErrorClass, classify

from . import connectors
from .registry import Capability, CapabilityRegistry, Health, Provider

_FAIL = re.compile(r"^(error|failed|couldn'?t|could not|unable to|i can'?t|permission denied|"
                   r"not found|no such|traceback)", re.I)


@dataclass
class RunResult:
    ok: bool
    outputs: list = field(default_factory=list)
    failed_step: Optional[int] = None
    error: str = ""
    error_class: str = ""

    def text(self) -> str:
        return self.outputs[-1] if self.outputs else ""


def _fill(value: Any, ctx: dict) -> Any:
    if isinstance(value, str):
        return re.sub(r"\{(\w+)\}", lambda m: str(ctx.get(m.group(1), m.group(0))), value)
    if isinstance(value, dict):
        return {k: _fill(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_fill(v, ctx) for v in value]
    return value


def _provider(cap: Capability, pid: str) -> Optional[Provider]:
    for p in cap.providers:
        if p.get("id") == pid:
            return Provider(**p)
    return None


def run(cap: Capability, inputs: dict, tool_exec: Callable[[str, dict, dict], Any], *,
        workflow: Optional[list] = None) -> RunResult:
    steps = workflow if workflow is not None else cap.workflow
    ctx = dict(inputs or {})
    out = RunResult(ok=True)
    for i, step in enumerate(steps, start=1):
        try:
            if "tool" in step:
                res = tool_exec(step["tool"], _fill(step.get("args", {}), ctx), {"capability": cap.id})
                text = res if isinstance(res, str) else json.dumps(res)[:4000]
                if _FAIL.match((text or "").strip()):
                    msg = text.strip()[:200]
                    return RunResult(False, out.outputs, i, msg, classify(msg).value)
            elif "provider" in step:
                prov = _provider(cap, step["provider"])
                if prov is None:
                    raise RuntimeError(f"provider {step['provider']} is not part of this capability")
                status, body = connectors.call_http(prov, step.get("method", "POST"),
                                                    _fill(step.get("path", "/"), ctx),
                                                    _fill(step.get("body"), ctx))
                if status in (401, 403):
                    raise PermissionError(f"{prov.name} needs to be reconnected")
                if not 200 <= status < 300:
                    return RunResult(False, out.outputs, i, f"{prov.name} answered {status}: {body[:160]}",
                                     classify(status=status).value)
                text = body
            else:
                raise RuntimeError("a step names neither a tool nor a provider")
        except PermissionError as e:
            return RunResult(False, out.outputs, i, f"auth_required: {e}", ErrorClass.AUTH.value)
        except Exception as e:
            return RunResult(False, out.outputs, i, str(e)[:300], classify(e).value)
        out.outputs.append(text)
        ctx[f"step{i}"] = text
    return out


def _check_expect(result: RunResult, expect: dict) -> tuple:
    text = result.text()
    if "contains" in expect and str(expect["contains"]).lower() not in text.lower():
        return False, f"output did not contain {expect['contains']!r}"
    if "min_length" in expect and len(text) < int(expect["min_length"]):
        return False, f"output was only {len(text)} characters"
    if expect.get("json"):
        try:
            json.loads(text)
        except Exception:
            return False, "output was not valid JSON"
    return True, "test passed"


def verify(registry: CapabilityRegistry, cap_id: str, tool_exec: Callable, *,
           version: Optional[int] = None) -> tuple:
    """Run the capability's test. Records the result; returns (passed, detail)."""
    cap = registry.get(cap_id)
    if cap is None:
        return False, f"no capability {cap_id}"
    test = cap.test or {}
    if not test:
        registry.record_test(cap_id, False, "no test declared, so it cannot be verified")
        return False, "no test declared, so it cannot be verified"
    wf = None
    if version is not None:
        v = next((v for v in cap.versions if v["number"] == version), None)
        if v is None:
            return False, f"no version {version}"
        wf = v["workflow"]
        staged = cap.staged_providers.get(str(version))
        if staged is not None:
            cap.providers = staged            # test the version with its own providers
    result = run(cap, test.get("inputs", {}), tool_exec, workflow=wf)
    if not result.ok:
        passed, detail = False, f"step {result.failed_step} failed: {result.error}"
        if result.error.startswith("auth_required"):
            registry.set_health(cap_id, Health.AUTH_REQUIRED, result.error)
        elif version is None and cap.learned and result.error_class in (
                ErrorClass.TRANSIENT.value, ErrorClass.UNAVAILABLE.value):
            # Could not reach a verdict. A slow or down service does not make
            # a working skill broken; its health says why it can't run now.
            h = Health.DEGRADED if result.error_class == ErrorClass.TRANSIENT.value else Health.UNAVAILABLE
            registry.record_inconclusive(cap_id, h, detail)
            return False, detail + " (inconclusive: " + result.error_class + ")"
    else:
        passed, detail = _check_expect(result, test.get("expect", {}))
    if version is not None:
        registry.record_version_test(cap_id, version, passed, detail)
    else:
        registry.record_test(cap_id, passed, detail)
    return passed, detail


def use(registry: CapabilityRegistry, cap_id: str, inputs: dict, tool_exec: Callable) -> RunResult:
    """Run a capability for real. Refuses one that is not learned or not healthy."""
    cap = registry.get(cap_id)
    if cap is None:
        return RunResult(False, error=f"no capability {cap_id}")
    if not cap.learned:
        return RunResult(False, error="this capability has not passed its test yet, so I won't rely on it")
    if cap.health not in (Health.AVAILABLE.value, Health.DEGRADED.value):
        return RunResult(False, error=f"this capability is {cap.health}")
    result = run(cap, inputs, tool_exec)
    if not result.ok and result.error.startswith("auth_required"):
        registry.set_health(cap_id, Health.AUTH_REQUIRED, result.error)
    elif not result.ok and result.error_class == ErrorClass.TRANSIENT.value:
        registry.set_health(cap_id, Health.DEGRADED, result.error)
    return result


__all__ = ["run", "verify", "use", "RunResult"]
