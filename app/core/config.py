from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _strip_env_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and ((value[0] == '"' and value[-1] == '"') or (value[0] == "'" and value[-1] == "'")):
        return value[1:-1]
    return value


def _load_dotenv_file() -> None:
    env_path = Path('.env')
    if not env_path.exists():
        return

    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(dotenv_path=env_path, override=False)
        return
    except Exception:
        pass

    for raw_line in env_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = _strip_env_value(value)


_load_dotenv_file()


@dataclass(slots=True)
class Settings:
    app_db_url: str = os.getenv('APP_DB_URL', 'sqlite+aiosqlite:///./data/agentic_browser.db')
    data_dir: Path = Path(os.getenv('DATA_DIR', './data'))
    screenshot_dir: Path = Path(os.getenv('SCREENSHOT_DIR', './data/screenshots'))
    profile_dir: Path = Path(os.getenv('PROFILE_DIR', './data/profiles'))
    browser_headless: bool = os.getenv('BROWSER_HEADLESS', 'false').lower() == 'true'
    browser_allowed_domains: list[str] = field(default_factory=lambda: [d.strip() for d in os.getenv('BROWSER_ALLOWED_DOMAINS', '').split(',') if d.strip()])
    browser_prohibited_domains: list[str] = field(default_factory=lambda: [d.strip() for d in os.getenv('BROWSER_PROHIBITED_DOMAINS', '').split(',') if d.strip()])
    sensitive_domains: list[str] = field(default_factory=lambda: [d.strip() for d in os.getenv('SENSITIVE_DOMAINS', '').split(',') if d.strip()])
    ollama_host: str = os.getenv('OLLAMA_HOST', 'http://localhost:11434')
    local_llm_host: str = os.getenv('LOCAL_LLM_HOST', 'http://127.0.0.1:11434')
    local_llm_model: str = os.getenv('LOCAL_LLM_MODEL', 'qwen3:0.6b')
    local_llm_timeout_s: float = float(os.getenv('LOCAL_LLM_TIMEOUT_S', '45'))
    local_llm_enrichment_enabled: bool = os.getenv('LOCAL_LLM_ENRICHMENT_ENABLED', 'true').lower() == 'true'
    browser_use_llm_provider: str = os.getenv('BROWSER_USE_LLM_PROVIDER', 'ollama').lower()
    ollama_model_planner: str = os.getenv('OLLAMA_MODEL_PLANNER', 'qwen2.5:7b')
    ollama_model_navigator: str = os.getenv('OLLAMA_MODEL_NAVIGATOR', 'qwen2.5:7b')
    ollama_model_extractor: str = os.getenv('OLLAMA_MODEL_EXTRACTOR', 'qwen2.5:7b')
    openai_api_key: str | None = os.getenv('OPENAI_API_KEY')
    openai_base_url: str | None = os.getenv('OPENAI_BASE_URL')
    openai_model_navigator: str = os.getenv('OPENAI_MODEL_NAVIGATOR', 'gpt-4o-mini')
    openai_model_planner: str = os.getenv('OPENAI_MODEL_PLANNER', 'gpt-4o-mini')
    openai_model_extractor: str = os.getenv('OPENAI_MODEL_EXTRACTOR', 'gpt-4o-mini')
    enable_screenshots: bool = os.getenv('ENABLE_SCREENSHOTS', 'true').lower() == 'true'
    action_rate_limit_ms: int = int(os.getenv('ACTION_RATE_LIMIT_MS', '1200'))
    max_step_retries: int = int(os.getenv('MAX_STEP_RETRIES', '2'))
    log_level: str = os.getenv('LOG_LEVEL', 'INFO')
    use_fake_browser_adapter: bool = os.getenv('USE_FAKE_BROWSER_ADAPTER', 'false').lower() == 'true'
    browser_backend: str = os.getenv('BROWSER_BACKEND', '').lower()
    browser_private_hosts: list[str] = field(default_factory=lambda: [
        host.strip().lower() for host in os.getenv('BROWSER_PRIVATE_HOSTS', '').split(',') if host.strip()
    ])
    browser_demo_url: str | None = os.getenv('BROWSER_DEMO_URL')
    browser_executable_path: str | None = os.getenv('BROWSER_EXECUTABLE_PATH')
    browser_sandbox: bool = os.getenv('BROWSER_SANDBOX', 'true').lower() == 'true'
    browser_max_branches: int = int(os.getenv('BROWSER_MAX_BRANCHES', '6'))
    browser_max_tabs: int = int(os.getenv('BROWSER_MAX_TABS', '8'))
    browser_timeout_ms: int = int(os.getenv('BROWSER_TIMEOUT_MS', '5000'))
    browser_artifact_limit_bytes: int = int(os.getenv('BROWSER_ARTIFACT_LIMIT_BYTES', '8388608'))
    browser_agent_provider: str = os.getenv('BROWSER_AGENT_PROVIDER', 'ollama').lower()
    browser_agent_model: str | None = os.getenv('BROWSER_AGENT_MODEL')
    browser_agent_visual: bool = os.getenv('BROWSER_AGENT_VISUAL', 'false').lower() == 'true'
    browser_agent_timeout_s: float = float(os.getenv('BROWSER_AGENT_TIMEOUT_S', '35'))
    browser_agent_max_steps: int = int(os.getenv('BROWSER_AGENT_MAX_STEPS', '40'))
    browser_local_model_profiles: dict[str, dict] = field(default_factory=lambda: json.loads(os.getenv('BROWSER_LOCAL_MODEL_PROFILES', '{}')))
    browser_model_parallelism: int = int(os.getenv('BROWSER_MODEL_PARALLELISM', '1'))
    browser_max_group_tasks: int = int(os.getenv('BROWSER_MAX_GROUP_TASKS', '6'))
    workspace_access_token: str | None = os.getenv('WORKSPACE_ACCESS_TOKEN')
    reverse_engineering_enabled: bool = os.getenv('REVERSE_ENGINEERING_ENABLED', 'false').lower() == 'true'
    workspace_lease_timeout_s: int = int(os.getenv('WORKSPACE_LEASE_TIMEOUT_S', '300'))
    workspace_hosts: list[str] = field(default_factory=lambda: [
        host.strip() for host in os.getenv('WORKSPACE_HOSTS', 'localhost,127.0.0.1').split(',') if host.strip()
    ])

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
