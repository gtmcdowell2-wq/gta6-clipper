"""One door to Claude for every agent.

What it adds over a bare API call:
- Server-side refusal fallback (beta `server-side-fallback-2026-07-01`, `fallbacks="default"`): if the main
  model declines a request, the API re-runs it on a fallback model inside the same call.
- A backup model for outages: when the main model is overloaded or unreachable after the SDK's own retries,
  the same request goes to `llm.backup_model` (set it to null to turn this off).
- A daily spend cap (`llm.daily_budget_usd`): past it, AI agents pause until midnight UTC instead of spending more.
- A usage log (table llm_usage) so the control center can show what the AI costs per day and per agent.
"""
from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .config import Config, secret

FALLBACK_BETA = "server-side-fallback-2026-07-01"

# USD per million tokens: input, output, cache read, cache write (5-minute). Anthropic list prices, Nov 2026.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5": (5.00, 25.00, 0.50, 6.25),
    "claude-opus-4-8": (5.00, 25.00, 0.50, 6.25),
    "claude-sonnet-5": (2.00, 10.00, 0.20, 2.50),
    "claude-fable-5-2": (8.00, 40.00, 0.20, 10.00),
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY, ts TEXT NOT NULL DEFAULT (datetime('now')), purpose TEXT NOT NULL, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER, cache_write INTEGER,
    cost_usd REAL, outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_ts ON llm_usage(ts);
"""


class LLMUnavailable(RuntimeError):
    """No answer now (no key, out of budget, outage). The caller should wait or use its backup method."""


class LLMDeclined(Exception):
    """The model (and its fallback) declined, or the answer was cut off. Don't retry the same request."""


@dataclass
class Answer:
    text: str
    model: str          # the model that actually answered
    cost_usd: float | None
    used_backup: bool   # our outage backup answered, not the main model
    rescued: bool       # the server-side refusal fallback answered


def settings(cfg: Config) -> dict:
    llm = dict(cfg.raw.get("llm") or {})
    llm.setdefault("model", cfg["selector"].get("model", "claude-opus-5-5"))
    llm.setdefault("backup_model", "claude-sonnet-5-5")
    llm.setdefault("daily_budget_usd", 30)
    llm.setdefault("refusal_fallback", True)
    return llm


def available(cfg: Config) -> tuple[bool, str]:
    if not secret("ANTHROPIC_API_KEY"):
        return False, "no ANTHROPIC_API_KEY in .env"
    return True, ""


def cost_of(model: str, usage) -> float | None:
    price = PRICES.get(model) or next((p for k, p in PRICES.items() if model and model.startswith(k)), None)
    if not price or usage is None:
        return None
    inp, out, read, write = price
    return round(((usage.input_tokens or 0) * inp + (usage.output_tokens or 0) * out
                  + (getattr(usage, "cache_read_input_tokens", 0) or 0) * read
                  + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * write) / 1e6, 5)


def spent_today(conn: sqlite3.Connection) -> float:
    conn.executescript(SCHEMA)
    return conn.execute("SELECT coalesce(sum(cost_usd), 0) FROM llm_usage WHERE ts >= date('now')").fetchone()[0]


def _record(conn, purpose, model, usage, cost, outcome):
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO llm_usage (purpose, model, input_tokens, output_tokens, cache_read, cache_write, cost_usd, outcome)"
                 " VALUES (?,?,?,?,?,?,?,?)",
                 (purpose, model, getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None),
                  getattr(usage, "cache_read_input_tokens", None), getattr(usage, "cache_creation_input_tokens", None),
                  cost, outcome))
    conn.commit()


def _content(user: str, images: list[Path] | None) -> list[dict] | str:
    if not images:
        return user
    blocks = []
    for p in images:
        media = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                   "data": base64.standard_b64encode(p.read_bytes()).decode()}})
    blocks.append({"type": "text", "text": user})
    return blocks


def ask(cfg: Config, conn: sqlite3.Connection, *, purpose: str, system: str, user: str, effort: str = "medium",
        max_tokens: int = 16000, images: list[Path] | None = None, model: str | None = None) -> Answer:
    import anthropic

    llm = settings(cfg)
    key = secret("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable("no ANTHROPIC_API_KEY in .env")
    budget = llm.get("daily_budget_usd")
    if budget and spent_today(conn) >= float(budget):
        raise LLMUnavailable(f"today's AI budget of ${float(budget):.0f} is used up (llm.daily_budget_usd in config.yaml)")

    primary = model or llm["model"]
    models = [primary] + ([llm["backup_model"]] if llm.get("backup_model") and llm["backup_model"] != primary else [])
    client = anthropic.Anthropic(api_key=key, max_retries=3, timeout=600)
    request = dict(max_tokens=max_tokens, thinking={"type": "adaptive"}, output_config={"effort": effort},
                   # The system prompt is the same on every call for a purpose, so cache it.
                   system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                   messages=[{"role": "user", "content": _content(user, images)}])
    use_fallback = bool(llm.get("refusal_fallback", True))
    problem = "no model tried"
    for i, name in enumerate(models):
        for attempt in range(2):  # second attempt only if the fallback beta itself was rejected
            try:
                if use_fallback:
                    r = client.beta.messages.create(model=name, betas=[FALLBACK_BETA], fallbacks="default", **request)
                else:
                    r = client.beta.messages.create(model=name, **request)
            except anthropic.AuthenticationError:
                raise LLMUnavailable("Anthropic rejected the API key: check ANTHROPIC_API_KEY in .env") from None
            except anthropic.PermissionDeniedError as e:
                raise LLMUnavailable(f"API key not allowed to use {name}: {e.message[:200]}") from None
            except (anthropic.PaymentRequiredError, anthropic.EnforcedSpendLimitReachedError,
                    anthropic.ContractSpendLimitReachedError, anthropic.OrganizationOnHoldError) as e:
                raise LLMUnavailable(f"Anthropic account can't be billed right now: {e.message[:200]}") from None
            except anthropic.BadRequestError as e:
                if use_fallback and "fallback" in str(e.message).lower():
                    use_fallback = False  # this account or model doesn't take the fallback beta; ask plainly
                    continue
                _record(conn, purpose, name, None, None, "bad_request")
                raise LLMDeclined(f"request rejected: {e.message[:300]}") from None
            except anthropic.NotFoundError:
                problem = f"model {name} not found (check llm.model in config.yaml)"
                break
            except (anthropic.RateLimitError, anthropic.OverloadedError, anthropic.InternalServerError,
                    anthropic.ServiceUnavailableError, anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
                problem = f"{name}: {type(e).__name__}"
                _record(conn, purpose, name, None, None, "unavailable")
                break  # the SDK already retried; move on to the backup model
            served = r.model or name
            cost = cost_of(served, r.usage)
            rescued = any(getattr(b, "type", "") == "fallback" for b in r.content)
            if r.stop_reason == "refusal":
                _record(conn, purpose, served, r.usage, cost, "refusal")
                category = getattr(getattr(r, "stop_details", None), "category", None)
                raise LLMDeclined(f"model declined{f' ({category})' if category else ''}")
            if r.stop_reason == "max_tokens":
                _record(conn, purpose, served, r.usage, cost, "cut_off")
                raise LLMDeclined("answer was cut off")
            _record(conn, purpose, served, r.usage, cost, "backup" if i else ("rescued" if rescued else "ok"))
            text = "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
            return Answer(text=text, model=served, cost_usd=cost, used_backup=bool(i), rescued=rescued)
    raise LLMUnavailable(f"Claude is unreachable right now ({problem}); will retry")


def usage_summary(conn: sqlite3.Connection) -> dict:
    conn.executescript(SCHEMA)
    today = conn.execute("SELECT coalesce(sum(cost_usd),0), count(*) FROM llm_usage WHERE ts >= date('now')").fetchone()
    week = conn.execute("SELECT coalesce(sum(cost_usd),0) FROM llm_usage WHERE ts >= date('now','-6 days')").fetchone()[0]
    by = conn.execute("SELECT purpose, count(*) n, coalesce(sum(cost_usd),0) usd FROM llm_usage WHERE ts >= date('now')"
                      " GROUP BY purpose ORDER BY usd DESC").fetchall()
    backups = conn.execute("SELECT count(*) FROM llm_usage WHERE ts >= date('now') AND outcome IN ('backup','rescued')").fetchone()[0]
    return {"today_usd": round(today[0], 2), "calls_today": today[1], "week_usd": round(week, 2),
            "backup_answers_today": backups, "by_purpose": [dict(r) for r in by]}
