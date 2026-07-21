"""Entry point: python -m agent"""

from agent.brain import AgentBrain
from agent.provider import ProviderError, create_provider, is_mock_provider
from agent.tools.registry import build_default_registry


def main() -> None:
    try:
        provider = create_provider()
    except ProviderError as exc:
        print(f"[agent] {exc}")
        raise SystemExit(1) from exc

    registry = build_default_registry(mock=is_mock_provider(provider))
    brain = AgentBrain(provider, registry=registry)
    brain.run_repl()


if __name__ == "__main__":
    main()
