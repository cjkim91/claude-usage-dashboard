"""Parse Claude Code local data: sessions, projects (JSONL), todos.

Config dirs:
  ~/.claude-personal/  — cdp (personal) sessions
  ~/.claude-work/      — cdw (work) sessions
  ~/.claude/           — shared base config (no sessions here)

Pure file I/O — no API calls.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Iterator

# Pricing per 1M tokens (USD). Updated for Claude 4 family.
# input / output / cache_write_5m / cache_write_1h / cache_read
PRICING = {
    "claude-opus-4-7":     {"in": 15.0,  "out": 75.0,  "cw5m": 18.75, "cw1h": 30.0, "cr": 1.5},
    "claude-opus-4-6":     {"in": 15.0,  "out": 75.0,  "cw5m": 18.75, "cw1h": 30.0, "cr": 1.5},
    "claude-opus-4-5":     {"in": 15.0,  "out": 75.0,  "cw5m": 18.75, "cw1h": 30.0, "cr": 1.5},
    "claude-sonnet-4-6":   {"in": 3.0,   "out": 15.0,  "cw5m": 3.75,  "cw1h": 6.0,  "cr": 0.3},
    "claude-sonnet-4-5":   {"in": 3.0,   "out": 15.0,  "cw5m": 3.75,  "cw1h": 6.0,  "cr": 0.3},
    "claude-haiku-4-5":    {"in": 1.0,   "out": 5.0,   "cw5m": 1.25,  "cw1h": 2.0,  "cr": 0.1},
}
# Strip suffixes like "[1m]" or version dates
def _normalize_model(m: str) -> str:
    if not m:
        return "unknown"
    base = m.split("[")[0]
    # claude-opus-4-7-20260101 → claude-opus-4-7
    parts = base.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit() and len(parts[1]) == 8:
        base = parts[0]
    return base


HOMES = [
    {"label": "work",     "root": Path.home() / ".claude-work"},
    {"label": "personal", "root": Path.home() / ".claude-personal"},
]


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_create: int = 0
    cache_read: int = 0

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_create += other.cache_create
        self.cache_read += other.cache_read

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_create + self.cache_read


def _proc_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _get_proc_env(pid: int) -> dict[str, str]:
    """Extract env vars from a running process via `ps eww`."""
    import re, subprocess
    try:
        r = subprocess.run(["ps", "eww", "-p", str(pid)], capture_output=True, text=True, timeout=3)
        env: dict[str, str] = {}
        for m in re.finditer(r"([A-Z_][A-Z0-9_]*)=([^\s]*)", r.stdout):
            env[m.group(1)] = m.group(2)
        return env
    except Exception:
        return {}


# Cache: surface_uuid → workspace_title (cleared on each full refresh)
_CMUX_TITLE_CACHE: dict[str, str] = {}
_CMUX_TREE_CACHE: dict[str, str] | None = None  # workspace_ref → title


def _cmux_workspace_titles() -> dict[str, str]:
    """workspace:N → title from `cmux tree --all`. Cached per process lifetime."""
    global _CMUX_TREE_CACHE
    if _CMUX_TREE_CACHE is not None:
        return _CMUX_TREE_CACHE
    import re, subprocess
    try:
        r = subprocess.run(["cmux", "tree", "--all"], capture_output=True, text=True, timeout=5)
        titles: dict[str, str] = {}
        for line in r.stdout.splitlines():
            m = re.search(r'workspace (workspace:\d+)\s+"([^"]+)"', line)
            if m:
                titles[m.group(1)] = m.group(2)
        _CMUX_TREE_CACHE = titles
        return titles
    except Exception:
        _CMUX_TREE_CACHE = {}
        return {}


def _cmux_session_name(pid: int) -> str | None:
    """Resolve cmux workspace title for a given claude process PID.

    Chain: PID → CMUX_SURFACE_ID (env) → `cmux identify --surface` → workspace_ref → title.
    """
    import re, subprocess
    env = _get_proc_env(pid)
    surface_id = env.get("CMUX_SURFACE_ID")
    if not surface_id:
        return None
    if surface_id in _CMUX_TITLE_CACHE:
        return _CMUX_TITLE_CACHE[surface_id]
    try:
        r = subprocess.run(["cmux", "identify", "--surface", surface_id],
                           capture_output=True, text=True, timeout=3)
        m = re.search(r'"workspace_ref"\s*:\s*"(workspace:\d+)"', r.stdout)
        if not m:
            return None
        ws_ref = m.group(1)
        titles = _cmux_workspace_titles()
        title = titles.get(ws_ref)
        if title:
            # Strip leading status icons (⠂ ⠐ ✳ etc.)
            title = re.sub(r"^[⠀-⣿✳⠂⠐●○◉]\s*", "", title).strip()
            _CMUX_TITLE_CACHE[surface_id] = title
        return title
    except Exception:
        return None


# ---- live sessions (from sessions/*.json) ----

def list_live_sessions() -> list[dict]:
    """Sessions currently alive (process exists)."""
    out = []
    for home in HOMES:
        sdir = home["root"] / "sessions"
        if not sdir.exists():
            continue
        for f in sdir.glob("*.json"):
            try:
                d = json.loads(f.read_text())
            except Exception:
                continue
            pid = d.get("pid")
            if not _proc_alive(pid):
                continue
            d["_home"] = home["label"]
            d["_pid_file"] = str(f)
            d["_session_name"] = _cmux_session_name(pid)
            out.append(d)
    return out


# ---- session message parsing (cached by mtime+size) ----

_PARSE_CACHE: dict[str, tuple[float, int, dict]] = {}


def _iter_jsonl(path: Path) -> Iterator[dict]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def _parse_jsonl_aggregate(path: Path) -> dict:
    """Lightweight aggregator over any JSONL (main or subagent)."""
    out = {
        "msg_counts": defaultdict(int),
        "model_usage": defaultdict(Usage),
        "tool_counts": defaultdict(int),
        "tool_usage": defaultdict(Usage),  # per-tool token attribution
        "skill_calls": [],     # [{t, skill, source}]
        "skill_counts": defaultdict(int),
        "usage_total": Usage(),
        "timeline": [],
        "events": [],          # chronological workflow events
        "first_user_text": None,
        "last_user_text": None,
        "last_assistant_text": None,
        "last_tool_use": None,
        "first_message_at": None,
        "last_message_at": None,
        "cwd": None,
        "git_branch": None,
        "version": None,
        "agent_calls": [],
        "active_agent_ids": set(),
    }

    for d in _iter_jsonl(path):
        t = d.get("type")
        ts = d.get("timestamp")
        if d.get("cwd") and not out["cwd"]:
            out["cwd"] = d["cwd"]
        if d.get("gitBranch") and not out["git_branch"]:
            out["git_branch"] = d["gitBranch"]
        if d.get("version") and not out["version"]:
            out["version"] = d["version"]
        if ts:
            out["last_message_at"] = ts
            if not out["first_message_at"]:
                out["first_message_at"] = ts

        if t == "user":
            out["msg_counts"]["user"] += 1
            msg = d.get("message", {})
            content = msg.get("content")
            text = None
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    if c.get("type") == "text":
                        text = c.get("text", "")
                        break
                    if c.get("type") == "tool_result":
                        tu_id = c.get("tool_use_id")
                        out["active_agent_ids"].discard(tu_id)
            if text:
                if not out["first_user_text"]:
                    out["first_user_text"] = text[:300]
                out["last_user_text"] = text[:300]
                # Slash command: stored as <command-name>/foo</command-name>
                import re
                cmd_match = re.search(r"<command-name>/?([A-Za-z][A-Za-z0-9:_-]{0,49})</command-name>", text)
                if cmd_match:
                    skill_name = cmd_match.group(1)
                    if skill_name not in ("exit", "compact"):  # filter trivial built-ins
                        out["skill_calls"].append({"t": ts, "skill": skill_name, "source": "slash"})
                        out["skill_counts"][skill_name] += 1
                    out["events"].append({"t": ts, "kind": "slash_command", "label": "/" + skill_name, "detail": text[:200]})
                else:
                    out["events"].append({"t": ts, "kind": "user", "label": "user prompt", "detail": text[:300]})

        elif t == "assistant":
            out["msg_counts"]["assistant"] += 1
            msg = d.get("message", {})
            model = _normalize_model(msg.get("model"))
            usage = msg.get("usage", {}) or {}
            u = Usage(
                input=usage.get("input_tokens", 0) or 0,
                output=usage.get("output_tokens", 0) or 0,
                cache_create=usage.get("cache_creation_input_tokens", 0) or 0,
                cache_read=usage.get("cache_read_input_tokens", 0) or 0,
            )
            out["usage_total"].add(u)
            out["model_usage"][model].add(u)

            if u.total > 0:
                out["timeline"].append({
                    "t": ts, "model": model,
                    "in": u.input, "out": u.output,
                    "cw": u.cache_create, "cr": u.cache_read,
                })

            # Collect tool_uses in this message — split this message's tokens across them
            tools_here: list[str] = []
            tool_events: list[dict] = []
            content = msg.get("content", [])
            for c in content:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "tool_use":
                    name = c.get("name", "?")
                    tools_here.append(name)
                    out["tool_counts"][name] += 1
                    out["last_tool_use"] = {"t": ts, "name": name, "id": c.get("id")}
                    if name in ("Agent", "Task"):
                        inp = c.get("input", {}) or {}
                        sub_type = inp.get("subagent_type") or "general-purpose"
                        desc = inp.get("description", "")[:160]
                        out["agent_calls"].append({
                            "t": ts,
                            "tool_use_id": c.get("id"),
                            "subagent": sub_type,
                            "description": desc,
                        })
                        out["active_agent_ids"].add(c.get("id"))
                        out["events"].append({"t": ts, "kind": "agent", "label": f"agent: {sub_type}", "detail": desc, "tool_use_id": c.get("id")})
                    elif name == "Skill":
                        inp = c.get("input", {}) or {}
                        skill_name = inp.get("skill") or "?"
                        out["skill_calls"].append({"t": ts, "skill": skill_name, "source": "tool"})
                        out["skill_counts"][skill_name] += 1
                        out["events"].append({"t": ts, "kind": "skill", "label": f"skill: {skill_name}", "detail": str(inp.get("args", ""))[:200]})
                    else:
                        inp = c.get("input", {}) or {}
                        detail = (inp.get("command") or inp.get("file_path") or
                                  inp.get("description") or inp.get("query") or
                                  inp.get("prompt") or "")
                        tool_events.append({"t": ts, "kind": "tool", "label": name,
                                            "detail": str(detail)[:120].replace("\n", " ").strip()})
                elif ctype == "text":
                    text = c.get("text", "")
                    if text:
                        out["last_assistant_text"] = text[:300]

            if tools_here:
                share_tok = u.total // len(tools_here) if len(tools_here) else 0
                share = Usage(
                    input=u.input // len(tools_here),
                    output=u.output // len(tools_here),
                    cache_create=u.cache_create // len(tools_here),
                    cache_read=u.cache_read // len(tools_here),
                )
                for tn in tools_here:
                    out["tool_usage"][tn].add(share)
                for te in tool_events:
                    out["events"].append({**te, "tokens": share_tok})

        elif t == "system":
            out["msg_counts"]["system"] += 1
        elif t == "attachment":
            out["msg_counts"]["attachment"] += 1

    return out


def _parse_subagents(session_dir: Path) -> list[dict]:
    """Parse <sessionId>/subagents/agent-*.{jsonl,meta.json} pairs."""
    sub_dir = session_dir / "subagents"
    if not sub_dir.exists():
        return []
    out = []
    for jsonl in sorted(sub_dir.glob("agent-*.jsonl")):
        meta_path = jsonl.with_suffix(".meta.json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                meta = {}
        agg = _parse_jsonl_aggregate(jsonl)
        try:
            mtime = jsonl.stat().st_mtime
            size = jsonl.stat().st_size
        except FileNotFoundError:
            continue
        out.append({
            "agent_id": jsonl.stem.replace("agent-", ""),
            "agent_type": meta.get("agentType") or "unknown",
            "description": meta.get("description") or "",
            "size": size,
            "mtime": mtime,
            "first_message_at": agg["first_message_at"],
            "last_message_at": agg["last_message_at"],
            "msg_counts": dict(agg["msg_counts"]),
            "tool_counts": dict(agg["tool_counts"]),
            "tool_usage": {k: asdict(v) for k, v in agg["tool_usage"].items()},
            "model_usage": {k: asdict(v) for k, v in agg["model_usage"].items()},
            "usage": asdict(agg["usage_total"]),
            "cost_usd": compute_cost({k: asdict(v) for k, v in agg["model_usage"].items()}),
            "events": agg["events"][:100],
            "_timeline": agg["timeline"],
        })
    return out


def parse_session(jsonl_path: Path) -> dict:
    """Parse a session JSONL file + its subagents/, return aggregated info.

    Cached by (mtime, size, subagents_dir_mtime).
    """
    try:
        st = jsonl_path.stat()
    except FileNotFoundError:
        return {}
    session_dir = jsonl_path.with_suffix("")  # /path/<sessionId>
    sub_dir = session_dir / "subagents"
    sub_mtime = sub_dir.stat().st_mtime if sub_dir.exists() else 0
    cached = _PARSE_CACHE.get(str(jsonl_path))
    if cached and cached[0] == st.st_mtime and cached[1] == st.st_size and cached[2].get("_sub_mtime") == sub_mtime:
        return cached[2]

    main_agg = _parse_jsonl_aggregate(jsonl_path)
    subagents = _parse_subagents(session_dir)

    # Merge subagent tokens into per-agent-type breakdown
    subagent_by_type: dict[str, dict] = defaultdict(lambda: {"usage": Usage(), "count": 0, "calls": []})
    sub_total = Usage()
    sub_tool_usage: dict[str, Usage] = defaultdict(Usage)
    sub_tool_counts: dict[str, int] = defaultdict(int)
    sub_model_usage: dict[str, Usage] = defaultdict(Usage)
    for s in subagents:
        atype = s["agent_type"]
        u = s["usage"]
        usg = Usage(input=u["input"], output=u["output"], cache_create=u["cache_create"], cache_read=u["cache_read"])
        subagent_by_type[atype]["usage"].add(usg)
        subagent_by_type[atype]["count"] += 1
        subagent_by_type[atype]["calls"].append({
            "agent_id": s["agent_id"],
            "description": s["description"],
            "usage": s["usage"],
            "cost_usd": s["cost_usd"],
            "msg_counts": s["msg_counts"],
            "tool_counts": s["tool_counts"],
            "first_message_at": s["first_message_at"],
            "last_message_at": s["last_message_at"],
        })
        sub_total.add(usg)
        for tn, tu in s["tool_usage"].items():
            sub_tool_usage[tn].add(Usage(**tu))
        for tn, tc in s["tool_counts"].items():
            sub_tool_counts[tn] += tc
        for m, mu in s["model_usage"].items():
            sub_model_usage[m].add(Usage(**mu))

    # Total = main thread + subagents
    total = Usage()
    total.add(main_agg["usage_total"])
    total.add(sub_total)

    # Combined model usage (main + sub)
    combined_model: dict[str, Usage] = defaultdict(Usage)
    for m, mu in main_agg["model_usage"].items():
        combined_model[m].add(mu)
    for m, mu in sub_model_usage.items():
        combined_model[m].add(mu)

    # Combined tool usage / counts
    combined_tool_counts: dict[str, int] = defaultdict(int)
    for tn, tc in main_agg["tool_counts"].items():
        combined_tool_counts[tn] += tc
    for tn, tc in sub_tool_counts.items():
        combined_tool_counts[tn] += tc
    combined_tool_usage: dict[str, Usage] = defaultdict(Usage)
    for tn, tu in main_agg["tool_usage"].items():
        combined_tool_usage[tn].add(tu)
    for tn, tu in sub_tool_usage.items():
        combined_tool_usage[tn].add(tu)

    info = {
        "session_id": jsonl_path.stem,
        "path": str(jsonl_path),
        "size": st.st_size,
        "mtime": st.st_mtime,
        "_sub_mtime": sub_mtime,
        "cwd": main_agg["cwd"],
        "git_branch": main_agg["git_branch"],
        "version": main_agg["version"],
        "first_user_text": main_agg["first_user_text"],
        "last_user_text": main_agg["last_user_text"],
        "last_assistant_text": main_agg["last_assistant_text"],
        "last_tool_use": main_agg["last_tool_use"],
        "first_message_at": main_agg["first_message_at"],
        "last_message_at": main_agg["last_message_at"],
        "msg_counts": dict(main_agg["msg_counts"]),
        # Main-thread only
        "usage_main": asdict(main_agg["usage_total"]),
        "model_usage_main": {k: asdict(v) for k, v in main_agg["model_usage"].items()},
        "tool_counts_main": dict(main_agg["tool_counts"]),
        "tool_usage_main": {k: asdict(v) for k, v in main_agg["tool_usage"].items()},
        # Subagents
        "subagents": subagents,
        "subagent_by_type": {
            k: {
                "usage": asdict(v["usage"]),
                "count": v["count"],
                "cost_usd": compute_cost({"_": asdict(v["usage"])}) if False else None,
                "calls": v["calls"],
            } for k, v in subagent_by_type.items()
        },
        "usage_subagents": asdict(sub_total),
        # Combined
        "usage_total": asdict(total),
        "model_usage": {k: asdict(v) for k, v in combined_model.items()},
        "tool_counts": dict(combined_tool_counts),
        "tool_usage": {k: asdict(v) for k, v in combined_tool_usage.items()},
        # Activity
        "agent_calls": main_agg["agent_calls"],
        "active_subagents": len(main_agg["active_agent_ids"]),
        "skill_calls": main_agg["skill_calls"],
        "skill_counts": dict(main_agg["skill_counts"]),
        "events": main_agg["events"],
        "timeline": _bucket_timeline(
            main_agg["timeline"]
            + [e for s in subagents for e in s.get("_timeline", [])]
        ),
    }
    # strip private fields from subagents before exposing
    for s in info["subagents"]:
        s.pop("_timeline", None)
    # Per-subagent-type cost
    for k, v in info["subagent_by_type"].items():
        # rough cost using combined model rates is harder w/o per-agent model split;
        # use the per-agent model split when available
        agent_models: dict[str, Usage] = defaultdict(Usage)
        for s in subagents:
            if s["agent_type"] != k:
                continue
            for m, mu in s["model_usage"].items():
                agent_models[m].add(Usage(**mu))
        v["cost_usd"] = compute_cost({m: asdict(u) for m, u in agent_models.items()})

    _PARSE_CACHE[str(jsonl_path)] = (st.st_mtime, st.st_size, info)
    return info


def _bucket_timeline(events: list[dict], bucket_seconds: int = 60) -> list[dict]:
    if not events:
        return []
    buckets: dict[int, dict] = {}
    for e in events:
        try:
            ts = datetime.fromisoformat(e["t"].replace("Z", "+00:00"))
        except Exception:
            continue
        b = int(ts.timestamp()) // bucket_seconds * bucket_seconds
        bk = buckets.setdefault(b, {"t": b, "in": 0, "out": 0, "cw": 0, "cr": 0, "models": set()})
        bk["in"] += e["in"]
        bk["out"] += e["out"]
        bk["cw"] += e["cw"]
        bk["cr"] += e["cr"]
        bk["models"].add(e["model"])
    out = []
    for b in sorted(buckets):
        bk = buckets[b]
        bk["models"] = sorted(bk["models"])
        out.append(bk)
    return out


# ---- list all sessions across both homes ----

def list_all_sessions(min_size: int = 0) -> list[dict]:
    """All session JSONL files across both homes, lightweight (no full parse)."""
    out = []
    for home in HOMES:
        pdir = home["root"] / "projects"
        if not pdir.exists():
            continue
        for proj_dir in pdir.iterdir():
            if not proj_dir.is_dir():
                continue
            for f in proj_dir.glob("*.jsonl"):
                try:
                    st = f.stat()
                except FileNotFoundError:
                    continue
                if st.st_size < min_size:
                    continue
                out.append({
                    "session_id": f.stem,
                    "home": home["label"],
                    "project_dir": proj_dir.name,
                    "path": str(f),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# ---- aggregate stats ----

def compute_cost(model_usage: dict) -> float:
    total = 0.0
    for model, u in model_usage.items():
        p = PRICING.get(model)
        if not p:
            continue
        total += (u["input"] / 1_000_000) * p["in"]
        total += (u["output"] / 1_000_000) * p["out"]
        total += (u["cache_create"] / 1_000_000) * p["cw1h"]  # assume 1h cache
        total += (u["cache_read"] / 1_000_000) * p["cr"]
    return total


def aggregate_stats(within_seconds: int | None = None) -> dict:
    """Total token usage across all sessions, optionally filtered to recent N seconds."""
    cutoff = time.time() - within_seconds if within_seconds else None
    total = Usage()
    by_model: dict[str, Usage] = defaultdict(Usage)
    by_home: dict[str, Usage] = defaultdict(Usage)
    sessions_counted = 0

    for s in list_all_sessions():
        if cutoff is not None and s["mtime"] < cutoff:
            # Still parse if we want intra-session time filter,
            # but for "recent activity" mtime is a good first-pass filter.
            # We'll still parse to get accurate per-message timestamps.
            pass
        info = parse_session(Path(s["path"]))
        if not info.get("usage_total"):
            continue
        sessions_counted += 1
        for model, u in info["model_usage"].items():
            mu = by_model[model]
            mu.input += u["input"]
            mu.output += u["output"]
            mu.cache_create += u["cache_create"]
            mu.cache_read += u["cache_read"]
            hu = by_home[s["home"]]
            hu.input += u["input"]
            hu.output += u["output"]
            hu.cache_create += u["cache_create"]
            hu.cache_read += u["cache_read"]
            total.input += u["input"]
            total.output += u["output"]
            total.cache_create += u["cache_create"]
            total.cache_read += u["cache_read"]

    by_model_d = {k: asdict(v) for k, v in by_model.items()}
    return {
        "total": asdict(total),
        "by_model": by_model_d,
        "by_home": {k: asdict(v) for k, v in by_home.items()},
        "cost_usd": compute_cost(by_model_d),
        "sessions_counted": sessions_counted,
    }


def aggregate_by_model() -> dict:
    """All-time token usage by model, with per-session breakdown for drill-down."""
    live_ids = {s.get("sessionId") for s in list_live_sessions()}
    by_model: dict[str, dict] = {}

    for s in list_all_sessions():
        info = parse_session(Path(s["path"]))
        for model, mu in (info.get("model_usage") or {}).items():
            entry = by_model.setdefault(model, {"total": Usage(), "sessions": []})
            entry["total"].add(Usage(**mu))
            tok = mu["input"] + mu["output"] + mu["cache_create"] + mu["cache_read"]
            if tok > 0:
                entry["sessions"].append({
                    "session_id": s["session_id"],
                    "home": s["home"],
                    "is_live": s["session_id"] in live_ids,
                    "cwd": info.get("cwd"),
                    "first_user_text": info.get("first_user_text"),
                    "last_message_at": info.get("last_message_at"),
                    "tokens": tok,
                    "mtime": s["mtime"],
                })

    return {
        model: {
            "total": asdict(v["total"]),
            "sessions": sorted(v["sessions"], key=lambda x: -(x["tokens"])),
        }
        for model, v in by_model.items()
    }


def aggregate_by_subagent_and_skill() -> dict:
    """Across all sessions: tokens grouped by subagent_type and skill name.

    Subagents have real token counts (their own JSONL).
    Skills only have call counts (no separate token attribution — they run inline).
    """
    by_subagent_type: dict[str, dict] = defaultdict(lambda: {"tokens": Usage(), "count": 0, "sessions": set()})
    skill_counts: dict[str, int] = defaultdict(int)
    skill_sessions: dict[str, set] = defaultdict(set)

    for s in list_all_sessions():
        info = parse_session(Path(s["path"]))
        for sub in info.get("subagents", []) or []:
            atype = sub["agent_type"]
            u = sub["usage"]
            usg = Usage(input=u["input"], output=u["output"], cache_create=u["cache_create"], cache_read=u["cache_read"])
            by_subagent_type[atype]["tokens"].add(usg)
            by_subagent_type[atype]["count"] += 1
            by_subagent_type[atype]["sessions"].add(s["session_id"])
        for skill, cnt in (info.get("skill_counts") or {}).items():
            skill_counts[skill] += cnt
            skill_sessions[skill].add(s["session_id"])

    live_ids = {s.get("sessionId") for s in list_live_sessions()}
    sub_session_lists: dict[str, list] = defaultdict(list)
    skill_session_lists: dict[str, list] = defaultdict(list)
    for s in list_all_sessions():
        info = parse_session(Path(s["path"]))
        for sub in info.get("subagents", []) or []:
            atype = sub["agent_type"]
            u = sub["usage"]
            tok = u["input"]+u["output"]+u["cache_create"]+u["cache_read"]
            sub_session_lists[atype].append({
                "session_id": s["session_id"],
                "home": s["home"],
                "is_live": s["session_id"] in live_ids,
                "cwd": info.get("cwd"),
                "first_user_text": info.get("first_user_text"),
                "last_message_at": info.get("last_message_at"),
                "description": sub["description"],
                "tokens": tok,
                "mtime": s["mtime"],
            })
        for skill, cnt in (info.get("skill_counts") or {}).items():
            skill_session_lists[skill].append({
                "session_id": s["session_id"],
                "home": s["home"],
                "is_live": s["session_id"] in live_ids,
                "cwd": info.get("cwd"),
                "first_user_text": info.get("first_user_text"),
                "last_message_at": info.get("last_message_at"),
                "count": cnt,
                "mtime": s["mtime"],
            })

    return {
        "subagents": {
            k: {
                "tokens": asdict(v["tokens"]),
                "count": v["count"],
                "session_count": len(v["sessions"]),
                "sessions": sorted(sub_session_lists.get(k, []), key=lambda x: -x["mtime"]),
            } for k, v in by_subagent_type.items()
        },
        "skills": {
            k: {
                "count": v,
                "session_count": len(skill_sessions[k]),
                "sessions": sorted(skill_session_lists.get(k, []), key=lambda x: -x["mtime"]),
            }
            for k, v in skill_counts.items()
        },
    }


_OAUTH_CACHE: dict = {}  # {"data": ..., "cached_at": float}
_OAUTH_CACHE_TTL = 300  # 5 minutes — avoids 429s


def _find_best_oauth_token() -> str | None:
    """Scan all Claude Code-credentials-* keychain entries; return freshest non-expired token."""
    import subprocess, re
    try:
        dump = subprocess.run(
            ["security", "dump-keychain"],
            capture_output=True, text=True, timeout=5
        ).stdout
        services = re.findall(r'"svce"<blob>="(Claude Code-credentials[^"]*)"', dump)
        services = list(dict.fromkeys(services))  # deduplicate, preserve order
    except Exception:
        services = ["Claude Code-credentials"]

    best_tok: str | None = None
    best_exp: float = 0.0
    now_ts = time.time()

    for svc in services:
        try:
            raw = subprocess.run(
                ["security", "find-generic-password", "-s", svc, "-w"],
                capture_output=True, text=True, timeout=5
            ).stdout.strip()
            d = json.loads(raw)
            oauth = d.get("claudeAiOauth", {})
            tok = oauth.get("accessToken", "")
            exp_ms = oauth.get("expiresAt", 0) or 0
            exp_s = exp_ms / 1000
            if tok and exp_s > now_ts and exp_s > best_exp:
                best_tok = tok
                best_exp = exp_s
        except Exception:
            continue
    return best_tok


def fetch_oauth_usage() -> dict | None:
    """Fetch official plan utilization from Anthropic's OAuth usage endpoint.

    Returns {"five_hour": {"utilization": float, "resets_at": str}, ...} or None on failure.
    Caches result for 5 minutes to avoid rate-limiting.
    macOS-only (reads from keychain). Silently returns None on other platforms.
    """
    import platform
    if platform.system() != "Darwin":
        return None

    now = time.time()
    cached = _OAUTH_CACHE.get("data")
    if cached and now - _OAUTH_CACHE.get("cached_at", 0) < _OAUTH_CACHE_TTL:
        return cached

    token = _find_best_oauth_token()
    if not token:
        return None

    import urllib.request, urllib.error
    req = urllib.request.Request(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
        _OAUTH_CACHE["data"] = data
        _OAUTH_CACHE["cached_at"] = now
        return data
    except Exception:
        return None


def rolling_5h_usage() -> dict:
    """Token usage in the last 5 hours (Anthropic plan rolling window).

    Reset time = first message in the window + 5h.
    Uses raw JSONL timestamps (ms precision) and user-message timestamps
    (≈ API request time) for first_msg_in_window, which is closer to
    what Anthropic's servers see than assistant-response timestamps.
    """
    now = datetime.now(timezone.utc)
    five_h_ago = now - timedelta(hours=5)
    by_model: dict[str, Usage] = defaultdict(Usage)
    first_msg_in_window: datetime | None = None
    msg_count = 0

    def _scan_jsonl(path: Path) -> None:
        nonlocal first_msg_in_window, msg_count
        for d in _iter_jsonl(path):
            ts = d.get("timestamp")
            if not ts:
                continue
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if t < five_h_ago:
                continue
            # Track earliest message of any type (user msg ≈ API request time)
            if first_msg_in_window is None or t < first_msg_in_window:
                first_msg_in_window = t
            # Count tokens from assistant messages only
            if d.get("type") == "assistant":
                msg = d.get("message", {}) or {}
                model = _normalize_model(msg.get("model"))
                usage = msg.get("usage", {}) or {}
                u = by_model[model]
                u.input += usage.get("input_tokens", 0) or 0
                u.output += usage.get("output_tokens", 0) or 0
                u.cache_create += usage.get("cache_creation_input_tokens", 0) or 0
                u.cache_read += usage.get("cache_read_input_tokens", 0) or 0
                msg_count += 1

    for s in list_all_sessions():
        # Quick filter: skip sessions whose mtime is older than 5h
        if datetime.fromtimestamp(s["mtime"], tz=timezone.utc) < five_h_ago:
            continue
        path = Path(s["path"])
        _scan_jsonl(path)
        # Also scan subagent JSOLs
        sub_dir = path.with_suffix("") / "subagents"
        if sub_dir.exists():
            for sub_jsonl in sorted(sub_dir.glob("agent-*.jsonl")):
                try:
                    sub_mtime = sub_jsonl.stat().st_mtime
                except FileNotFoundError:
                    continue
                if datetime.fromtimestamp(sub_mtime, tz=timezone.utc) < five_h_ago:
                    continue
                _scan_jsonl(sub_jsonl)

    by_model_d = {k: asdict(v) for k, v in by_model.items()}
    total = Usage()
    for v in by_model.values():
        total.add(v)

    reset_at = None
    if first_msg_in_window is not None:
        reset_at = (first_msg_in_window + timedelta(hours=5)).isoformat()

    return {
        "total": asdict(total),
        "by_model": by_model_d,
        "cost_usd": compute_cost(by_model_d),
        "msg_count": msg_count,
        "first_message_at": first_msg_in_window.isoformat() if first_msg_in_window else None,
        "reset_at": reset_at,
        "now": now.isoformat(),
    }


def windowed_stats() -> dict:
    """Today / this week / all-time totals, computed from per-message timestamps."""
    now = datetime.now(timezone.utc)
    today_start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    week_start = today_start - timedelta(days=now.weekday())

    windows = {
        "today": {"start": today_start, "by_model": defaultdict(Usage)},
        "week":  {"start": week_start,  "by_model": defaultdict(Usage)},
        "all":   {"start": None,        "by_model": defaultdict(Usage)},
    }

    for s in list_all_sessions():
        info = parse_session(Path(s["path"]))
        for ev in info.get("timeline", []):
            try:
                t = datetime.fromtimestamp(ev["t"], tz=timezone.utc)
            except Exception:
                continue
            for w in windows.values():
                if w["start"] is None or t >= w["start"]:
                    # Find a model from the bucket; use first if multiple
                    model = (ev.get("models") or ["unknown"])[0]
                    u = w["by_model"][model]
                    u.input += ev["in"]
                    u.output += ev["out"]
                    u.cache_create += ev["cw"]
                    u.cache_read += ev["cr"]

    out = {}
    for name, w in windows.items():
        bm = {k: asdict(v) for k, v in w["by_model"].items()}
        total = Usage()
        for v in w["by_model"].values():
            total.add(v)
        out[name] = {
            "total": asdict(total),
            "by_model": bm,
            "cost_usd": compute_cost(bm),
        }
    return out


# ---- session enrichment for dashboard list ----

def session_summary(session_id: str, jsonl_path: Path, home: str, is_live: bool, live_meta: dict | None) -> dict:
    info = parse_session(jsonl_path)
    return {
        "session_id": session_id,
        "home": home,
        "is_live": is_live,
        "pid": (live_meta or {}).get("pid"),
        "status": (live_meta or {}).get("status"),
        "kind": (live_meta or {}).get("kind"),
        "started_at": (live_meta or {}).get("startedAt"),
        "updated_at": (live_meta or {}).get("updatedAt"),
        "session_name": (live_meta or {}).get("_session_name"),
        "version": info.get("version"),
        "cwd": info.get("cwd"),
        "git_branch": info.get("git_branch"),
        "first_user_text": info.get("first_user_text"),
        "last_user_text": info.get("last_user_text"),
        "last_assistant_text": info.get("last_assistant_text"),
        "last_tool_use": info.get("last_tool_use"),
        "last_message_at": info.get("last_message_at"),
        "msg_counts": info.get("msg_counts"),
        "tool_counts": info.get("tool_counts"),
        "agent_calls_count": len(info.get("agent_calls") or []),
        "active_subagents": info.get("active_subagents", 0),
        "subagents_count": len(info.get("subagents") or []),
        "subagent_types": list((info.get("subagent_by_type") or {}).keys()),
        "usage_total": info.get("usage_total"),
        "usage_main": info.get("usage_main"),
        "usage_subagents": info.get("usage_subagents"),
        "model_usage": info.get("model_usage"),
        "cost_usd": compute_cost(info.get("model_usage") or {}),
        "size": info.get("size"),
        "mtime": info.get("mtime"),
    }


def list_sessions_enriched(limit: int = 50) -> list[dict]:
    live = {s.get("sessionId"): s for s in list_live_sessions() if s.get("sessionId")}
    seen_ids = set()
    rows = []

    # Live sessions first (always include even if no JSONL yet)
    for sid, lv in live.items():
        # Find matching JSONL
        path = None
        home_label = lv.get("_home")
        for h in HOMES:
            if h["label"] != home_label:
                continue
            for proj in (h["root"] / "projects").glob("*"):
                p = proj / f"{sid}.jsonl"
                if p.exists():
                    path = p
                    break
            if path:
                break
        if path:
            rows.append(session_summary(sid, path, home_label, True, lv))
        else:
            rows.append({
                "session_id": sid,
                "home": home_label,
                "is_live": True,
                "pid": lv.get("pid"),
                "status": lv.get("status"),
                "kind": lv.get("kind"),
                "started_at": lv.get("startedAt"),
                "cwd": lv.get("cwd"),
                "version": lv.get("version"),
                "msg_counts": {},
                "usage_total": asdict(Usage()),
                "model_usage": {},
                "cost_usd": 0.0,
            })
        seen_ids.add(sid)

    # Recent sessions
    for s in list_all_sessions():
        if s["session_id"] in seen_ids:
            continue
        rows.append(session_summary(
            s["session_id"], Path(s["path"]), s["home"], False, None
        ))
        seen_ids.add(s["session_id"])
        if len(rows) >= limit:
            break

    rows.sort(key=lambda r: (not r["is_live"], -(r.get("mtime") or 0)))
    return rows


def session_detail(session_id: str) -> dict | None:
    """Find session JSONL by id and return full parsed info plus live meta if any."""
    live = None
    for s in list_live_sessions():
        if s.get("sessionId") == session_id:
            live = s
            break

    for h in HOMES:
        for proj in (h["root"] / "projects").glob("*"):
            p = proj / f"{session_id}.jsonl"
            if p.exists():
                info = parse_session(p)
                info["home"] = h["label"]
                info["is_live"] = live is not None
                info["live_meta"] = live
                info["cost_usd"] = compute_cost(info.get("model_usage") or {})
                return info
    return None
