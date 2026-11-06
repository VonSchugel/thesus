from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from hashlib import sha256
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from app.agents.browser_planner import BrowserPlanner
from app.core.config import Settings
from app.core.errors import ConflictError, ValidationError


class LocalModelSpec(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)

    provider: Literal['ollama', 'llama_cpp'] = 'ollama'
    model: str = Field(min_length=1, max_length=160)
    label: str | None = Field(default=None, min_length=1, max_length=100)
    base_url: str | None = Field(default=None, max_length=500)
    visual: bool = False


@dataclass(slots=True)
class LocalModel:
    public: dict
    signature: str
    planner: BrowserPlanner


class BrowserModels:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.local: dict[str, LocalModel] = {}
        if not isinstance(settings.browser_local_model_profiles, dict):
            raise ValueError('BROWSER_LOCAL_MODEL_PROFILES must be a JSON object keyed by profile ID')
        if not 1 <= settings.browser_model_parallelism <= 16:
            raise ValueError('BROWSER_MODEL_PARALLELISM must be between 1 and 16')
        if not 2 <= settings.browser_max_group_tasks <= 24:
            raise ValueError('BROWSER_MAX_GROUP_TASKS must be between 2 and 24')
        for profile_id, raw in settings.browser_local_model_profiles.items():
            if profile_id == 'default' or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,47}', profile_id):
                raise ValueError('Local model profile IDs must use lowercase letters, numbers, hyphens or underscores')
            spec = LocalModelSpec.model_validate(raw)
            host = spec.base_url or (settings.ollama_host if spec.provider == 'ollama' else None)
            if not host or not self._loopback(host, spec.provider):
                raise ValueError(f'Local model profile {profile_id} requires a loopback HTTP endpoint')
            if spec.provider == 'ollama' and spec.model.lower().endswith(('-cloud', ':cloud')):
                raise ValueError(f'Local model profile {profile_id} references an Ollama cloud model')
            host = host.rstrip('/')
            public = {'id': profile_id, 'label': spec.label or profile_id, 'provider': spec.provider,
                      'model': spec.model, 'visual': spec.visual}
            signature = sha256(json.dumps([spec.provider, host, spec.model, spec.visual]).encode()).hexdigest()
            selected = replace(settings, browser_agent_model=spec.model, browser_agent_visual=spec.visual,
                               browser_agent_provider='ollama' if spec.provider == 'ollama' else 'openai',
                               ollama_host=host if spec.provider == 'ollama' else settings.ollama_host,
                               openai_base_url=host if spec.provider == 'llama_cpp' else None,
                               openai_api_key=None)
            self.local[profile_id] = LocalModel(public, signature, BrowserPlanner(selected))

    @staticmethod
    def _loopback(url: str, provider: str) -> bool:
        try:
            endpoint = urlsplit(url)
            port = endpoint.port
        except ValueError:
            return False
        return (endpoint.scheme == 'http' and endpoint.hostname in {'localhost', '127.0.0.1', '::1'}
                and endpoint.username is None and not endpoint.query and not endpoint.fragment and port != 0
                and endpoint.path.rstrip('/') == ('/v1' if provider == 'llama_cpp' else ''))

    def default(self) -> dict:
        provider = self.settings.browser_agent_provider
        model = None
        if provider != 'disabled':
            model = self.settings.browser_agent_model or (self.settings.ollama_model_navigator if provider == 'ollama'
                                                          else self.settings.openai_model_navigator)
        if provider == 'openai' and self.settings.openai_base_url and self._loopback(self.settings.openai_base_url, 'llama_cpp'):
            provider = 'llama_cpp'
        return {'id': 'default', 'label': 'Workspace default', 'provider': provider,
                'model': model, 'visual': self.settings.browser_agent_visual}

    def catalog(self) -> dict:
        return {'default': self.default(), 'profiles': [entry.public for entry in self.local.values()],
                'inference_parallelism': self.settings.browser_model_parallelism,
                'max_group_tasks': self.settings.browser_max_group_tasks}

    def selection(self, profile_id: str | None) -> dict:
        if profile_id in (None, 'default'):
            return {}
        entry = self.local.get(profile_id)
        if entry is None:
            raise ValidationError('Choose a model profile configured for this workspace')
        return {'model_selection': {'id': profile_id, 'name': entry.public['model'], 'signature': entry.signature}}

    def resolve(self, board: dict, default: BrowserPlanner) -> tuple[BrowserPlanner, dict]:
        selection = board.get('model_selection')
        if not selection:
            return default, self.default()
        entry = self.local.get(selection['id'])
        if entry is None or entry.signature != selection['signature']:
            raise ConflictError('This task’s local model profile changed. Restore that profile to resume the task.',
                                error_code='model_profile_changed')
        return entry.planner, entry.public
