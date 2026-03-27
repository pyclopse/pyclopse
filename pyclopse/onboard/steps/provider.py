"""Provider configuration step — add, edit, or remove LLM providers."""

from typing import Any
from .. import menu

# ---------------------------------------------------------------------------
# Provider catalogue
# ---------------------------------------------------------------------------

KNOWN_PROVIDERS = {
    "anthropic": {
        "label": "Anthropic (Claude)",
        "key_name": "ANTHROPIC_API_KEY",
        "key_hint": "sk-ant-...",
        "default_model": "claude-sonnet-4-6",
        "default_models": ["claude-sonnet-4-6", "claude-opus-4-6", "claude-haiku-4-5-20251001"],
        "config_key": "apiKey",
        "fastagent_provider": None,
        "default_concurrency": 3,
        "needs_url": False,
        "default_url": None,
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "key_name": "OPENAI_API_KEY",
        "key_hint": "sk-...",
        "default_model": "gpt-4o",
        "default_models": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
        "config_key": "apiKey",
        "fastagent_provider": None,
        "default_concurrency": 5,
        "needs_url": False,
        "default_url": None,
    },
    "minimax": {
        "label": "MiniMax (International)",
        "key_name": "MINIMAX_API_KEY",
        "key_hint": "your-minimax-api-key",
        "default_model": "MiniMax-M2.7",
        "default_models": ["MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"],
        "config_key": "api_key",
        "fastagent_provider": "generic",
        "default_concurrency": 10,
        "needs_url": True,
        "default_url": "https://api.minimax.io/v1",
    },
    "minimax-coding": {
        "label": "MiniMax Coding Plan (China region)",
        "key_name": "MINIMAX_CODING_API_KEY",
        "key_hint": "your-minimax-coding-api-key",
        "default_model": "MiniMax-M2.7",
        "default_models": ["MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M2.1", "MiniMax-M2"],
        "config_key": "api_key",
        "fastagent_provider": "generic",
        "default_concurrency": 10,
        "needs_url": True,
        "default_url": "https://api.minimaxi.com/v1",
    },
    "zai": {
        "label": "Z.ai (GLM) — Standard",
        "key_name": "ZAI_API_KEY",
        "key_hint": "your-z.ai-api-key",
        "default_model": "glm-5",
        "default_models": ["glm-5", "glm-4.7", "glm-4.6", "glm-4.5"],
        "config_key": "api_key",
        "fastagent_provider": "generic",
        "default_concurrency": 2,
        "needs_url": True,
        "default_url": "https://api.z.ai/api/paas/v4",
    },
    "zai-coding": {
        "label": "Z.ai (GLM) — Coding Plan",
        "key_name": "ZAI_CODING_API_KEY",
        "key_hint": "your-z.ai-coding-plan-api-key",
        "default_model": "glm-5",
        "default_models": ["glm-5", "glm-4.7", "glm-4.6", "glm-4.5"],
        "config_key": "api_key",
        "fastagent_provider": "generic",
        "default_concurrency": 2,
        "needs_url": True,
        "default_url": "https://api.z.ai/api/coding/paas/v4",
    },
    "generic": {
        "label": "Other OpenAI-compatible endpoint (Ollama, etc.)",
        "key_name": "GENERIC_API_KEY",
        "key_hint": "your-api-key  (or 'none' for local Ollama)",
        "default_model": "my-model",
        "default_models": [],
        "config_key": "api_key",
        "fastagent_provider": "generic",
        "default_concurrency": 5,
        "needs_url": True,
        "default_url": "http://localhost:11434/v1",
    },
}


# ---------------------------------------------------------------------------
# Live model discovery
# ---------------------------------------------------------------------------

def _fetch_available_models(base_url: str, api_key: str) -> list[str] | None:
    """Try GET {base_url}/models and return model IDs, or None on failure."""
    try:
        import httpx
        r = httpx.get(
            f"{base_url.rstrip('/')}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=8,
        )
        if r.status_code != 200:
            return None
        data = r.json()
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return [m["id"] for m in data["data"] if isinstance(m, dict) and m.get("id")]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _configure_single_provider(
    provider_id: str,
    existing: dict | None,
    secrets: dict,
    env: dict,
) -> tuple[dict, dict, dict]:
    """Interactively configure one provider. Returns updated (provider_cfg, secrets, env)."""
    pdef = KNOWN_PROVIDERS[provider_id]

    # --- API key ---
    key_name = pdef["key_name"]
    current_key_display = "[dim](already set)[/dim]" if key_name in env else ""
    menu.info(f"  Hint: looks like {pdef['key_hint']}")
    if pdef.get("needs_url") is False or provider_id != "generic":
        raw_key = menu.ask(
            f"API key {current_key_display}",
            default="" if not current_key_display else "<keep>",
        )
    else:
        raw_key = menu.ask(
            f"API key (or 'none' for local Ollama) {current_key_display}",
            default="none" if not current_key_display else "<keep>",
        )

    if raw_key and raw_key != "<keep>":
        if raw_key.lower() != "none":
            env[key_name] = raw_key
        secrets[key_name] = {"source": "env"}

    # --- Base URL (generic-backed providers) ---
    fallback_url = pdef.get("default_url") or "http://localhost:11434/v1"
    api_url = existing.get("api_url", fallback_url) if existing else fallback_url
    if pdef["needs_url"]:
        api_url = menu.ask("API base URL", default=api_url)

    # --- Models ---
    # Resolve the effective API key for live discovery (may be newly entered or existing)
    effective_api_key = env.get(key_name) or (existing or {}).get("api_key", "")
    fetched_models: list[str] | None = None
    if pdef["needs_url"] and effective_api_key and effective_api_key not in ("none", ""):
        menu.info("  Fetching available models from provider...")
        fetched_models = _fetch_available_models(api_url, effective_api_key)
        if fetched_models:
            menu.success(f"Found {len(fetched_models)} model(s).")
        else:
            menu.warn("Could not fetch models from provider — using known defaults.")

    existing_models: dict = existing.get("models", {}) if existing else {}
    menu.console.print()
    if existing_models:
        menu.info(f"  Current models: {', '.join(existing_models.keys())}")
        if not menu.confirm("Edit models?", default=False):
            models = existing_models
        else:
            models = _configure_models(provider_id, existing_models, fetched_models)
    else:
        models = _configure_models(provider_id, {}, fetched_models)

    # --- Build config block ---
    cfg: dict[str, Any] = {"enabled": True}

    if provider_id == "anthropic":
        cfg["apiKey"] = f"${{{key_name}}}"
    elif provider_id == "openai":
        cfg["apiKey"] = f"${{{key_name}}}"
    else:
        cfg["fastagent_provider"] = pdef["fastagent_provider"]
        cfg["api_key"] = f"${{{key_name}}}" if key_name in env else (existing or {}).get("api_key", "none")
        cfg["api_url"] = api_url

    cfg["models"] = models
    return cfg, secrets, env


def _configure_models(
    provider_id: str,
    existing: dict,
    fetched_models: list[str] | None = None,
) -> dict:
    """Configure models for a provider. Returns models dict.

    Priority for the selectable list:
      1. fetched_models  — live from provider /models endpoint
      2. default_models  — curated list in KNOWN_PROVIDERS
      3. free-form entry — fallback when neither is available
    """
    pdef = KNOWN_PROVIDERS[provider_id]
    models: dict = dict(existing)

    menu.section("Models", style="cyan")

    candidate_list: list[str] = fetched_models or pdef["default_models"]

    if candidate_list:
        source = "from provider" if fetched_models else "recommended"
        menu.info(f"  Available models ({source}):")
        for i, m in enumerate(candidate_list, 1):
            currently_on = m in models
            is_default = m == pdef.get("default_model")
            status = "[green]✓[/green]" if currently_on else f"[dim]{i}[/dim]"
            tag = "  ← default" if is_default else ""
            menu.info(f"    [{status}]  {m}{tag}")

        menu.console.print()
        default_selection = ",".join(models.keys()) if models else (pdef.get("default_model") or candidate_list[0])
        raw = menu.ask(
            "Models to enable (comma-separated names or numbers, or 'all')",
            default=default_selection,
        )

        if raw.strip().lower() == "all":
            selected = candidate_list
        else:
            selected = []
            for token in raw.split(","):
                token = token.strip()
                if not token:
                    continue
                if token.isdigit():
                    idx = int(token) - 1
                    if 0 <= idx < len(candidate_list):
                        selected.append(candidate_list[idx])
                else:
                    selected.append(token)

        models = {}
        for m in selected:
            existing_cfg = existing.get(m, {})
            concurrency = existing_cfg.get("concurrency", pdef["default_concurrency"])
            models[m] = {"enabled": True, "concurrency": concurrency}
    else:
        # No list available — free-form entry
        raw = menu.ask(
            "Model name(s) to enable (comma-separated)",
            default=",".join(models.keys()) if models else pdef.get("default_model", "my-model"),
        )
        selected = [m.strip() for m in raw.split(",") if m.strip()]
        models = {}
        for m in selected:
            existing_cfg = existing.get(m, {})
            models[m] = {"enabled": True, "concurrency": existing_cfg.get("concurrency", pdef["default_concurrency"])}

    return models


# ---------------------------------------------------------------------------
# Public step
# ---------------------------------------------------------------------------

def step_providers(config: dict, secrets: dict, env: dict) -> tuple[dict, dict, dict]:
    """Interactively configure providers section.

    Supports add, edit, and remove on top of any existing config.
    Returns updated (config, secrets, env).
    """
    menu.section("Providers")

    if "providers" not in config:
        config["providers"] = {}

    providers = config["providers"]

    while True:
        # Show current state
        if providers:
            menu.info("  Configured providers:")
            for pid, pcfg in providers.items():
                label = KNOWN_PROVIDERS.get(pid, {}).get("label", pid)
                models = list(pcfg.get("models", {}).keys())
                menu.info(f"    [bold]{pid}[/bold]  ({label})  models: {', '.join(models) or 'none'}")
        else:
            menu.info("  No providers configured yet.")
        menu.console.print()

        options = [("add", "Add a provider")]
        if providers:
            options += [("edit", "Edit an existing provider")]
            options += [("remove", "Remove a provider")]
        options += [("done", "Done with providers")]

        action = menu.choose("Action", options, default="add" if not providers else "done")

        if action == "done":
            break

        elif action == "add":
            menu.section("Add Provider")
            available = [(pid, pdef["label"]) for pid, pdef in KNOWN_PROVIDERS.items() if pid not in providers]
            if not available:
                menu.warn("All known providers already configured.")
                continue
            # Add "other" option for custom provider IDs
            available.append(("__custom__", "Other (enter provider ID manually)"))
            pid = menu.choose("Choose provider", available)
            if pid == "__custom__":
                pid = menu.ask("Provider ID")
                if not pid:
                    continue
                # Treat as generic
                if pid not in KNOWN_PROVIDERS:
                    KNOWN_PROVIDERS[pid] = {**KNOWN_PROVIDERS["generic"], "label": pid, "key_name": f"{pid.upper()}_API_KEY"}
            cfg, secrets, env = _configure_single_provider(pid, None, secrets, env)
            providers[pid] = cfg
            menu.success(f"Provider '{pid}' added.")

        elif action == "edit":
            pid_options = [(pid, pid) for pid in providers]
            pid = menu.choose("Which provider?", pid_options)
            menu.section(f"Edit Provider: {pid}")
            cfg, secrets, env = _configure_single_provider(pid, providers[pid], secrets, env)
            providers[pid] = cfg
            menu.success(f"Provider '{pid}' updated.")

        elif action == "remove":
            pid_options = [(pid, pid) for pid in providers]
            pid = menu.choose("Which provider to remove?", pid_options)
            if menu.confirm(f"Remove provider '{pid}'?", default=False):
                del providers[pid]
                menu.success(f"Provider '{pid}' removed.")

    config["providers"] = providers
    return config, secrets, env
