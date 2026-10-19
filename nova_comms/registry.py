"""Provider registry for NOVA communications."""

from typing import Dict, Type, Optional
from .provider import ICommunicationProvider


class ProviderRegistry:
    _providers: Dict[str, Type[ICommunicationProvider]] = {}
    _instances: Dict[str, ICommunicationProvider] = {}

    @classmethod
    def register(cls, provider_class: Type[ICommunicationProvider]) -> None:
        name = provider_class().provider_name.lower()
        cls._providers[name] = provider_class

    @classmethod
    def get_provider(cls, name: str) -> Optional[Type[ICommunicationProvider]]:
        return cls._providers.get(name.lower())

    @classmethod
    def get_instance(cls, name: str, **kwargs) -> Optional[ICommunicationProvider]:
        key = name.lower()
        if key not in cls._instances:
            provider_class = cls.get_provider(key)
            if provider_class:
                cls._instances[key] = provider_class(**kwargs)
        return cls._instances.get(key)

    @classmethod
    def list_providers(cls) -> Dict[str, Type[ICommunicationProvider]]:
        return cls._providers.copy()

    @classmethod
    def clear_instances(cls) -> None:
        cls._instances.clear()