from __future__ import annotations

from .models import Decision
from .providers import DecisionProvider


class Controller:
    def __init__(self, provider: DecisionProvider) -> None:
        self.provider = provider

    def decide(self, context: str) -> Decision:
        return self.provider.decide(context)
