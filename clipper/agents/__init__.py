"""The agent network. Order here is the order shown in the control center."""
from .base import CREWS, Agent, Orchestrator
from .intake import Downloader, Editor, MomentPicker, Scout, SignalAnalyst, Transcriber
from .ops import Janitor, Supervisor
from .publishing import GrowthAnalyst, Publisher, StatsTracker, TimingPlanner
from .quality import Gatekeeper, HookCritic, Originality, PolicyGuard, ReachForecaster, TechQC

AGENT_CLASSES = [Scout, Downloader, Transcriber, SignalAnalyst, MomentPicker, Editor,
                 TechQC, PolicyGuard, Originality, HookCritic, ReachForecaster, Gatekeeper,
                 TimingPlanner, Publisher, StatsTracker, GrowthAnalyst,
                 Janitor, Supervisor]


def build_agents(cfg) -> list[Agent]:
    return [cls(cfg) for cls in AGENT_CLASSES]
