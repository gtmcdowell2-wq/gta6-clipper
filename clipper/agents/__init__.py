"""The agent network. The order here is the order the control center shows them in."""
from .base import CREWS, Agent, Ctx, Orchestrator
from .campaign import CAMPAIGN
from .growth import GROWTH
from .inspectors import INSPECTORS
from .intake import INTAKE, Scout
from .ops import OPS
from .publishing import build_publishing
from .quality import JUDGES


def build_agents(cfg) -> list[Agent]:
    agents: list[Agent] = [Scout(cfg, s) for s in cfg.sources]
    agents += [cls(cfg) for cls in INTAKE]
    agents += [cls(cfg) for cls in INSPECTORS]
    agents += [cls(cfg) for cls in JUDGES]
    agents += build_publishing(cfg)
    agents += [cls(cfg) for cls in CAMPAIGN]
    agents += [cls(cfg) for cls in GROWTH]
    agents += [cls(cfg) for cls in OPS]
    names = [a.name for a in agents]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise RuntimeError(f"two agents share a name: {', '.join(sorted(dupes))}")
    return agents


__all__ = ["CREWS", "Agent", "Ctx", "Orchestrator", "build_agents"]
