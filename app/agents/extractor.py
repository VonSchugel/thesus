from __future__ import annotations

from typing import Any

from app.integrations.local_llm import LocalLLMProvider
from app.models.domain import TaskPlan


class ExtractorAgent:
    def __init__(self, *, local_provider: LocalLLMProvider | None = None):
        self.local_provider = local_provider

    async def summarize(self, *, user_request: str, blackboard: dict[str, Any], plan: TaskPlan | None) -> tuple[dict[str, Any], str]:
        findings = list((blackboard or {}).get('findings', []))
        items: list[dict[str, Any]] = []
        for idx, item in enumerate(findings[:3], start=1):
            items.append(
                {
                    'rank': idx,
                    'name': item.get('title') or f'Candidate {idx}',
                    'price': item.get('price') or item.get('observation') or 'unknown',
                    'url': item.get('url'),
                    'pros': item.get('pros', []),
                    'cons': item.get('cons', []),
                }
            )
        structured = {
            'request': user_request,
            'goal': plan.goal if plan else user_request,
            'items': items,
            'notes': list((blackboard or {}).get('notes', [])),
        }
        if items:
            text = 'Comparison ready: ' + '; '.join(f"#{i['rank']} {i['name']} ({i['price']})" for i in items)
        else:
            text = 'No extracted items yet. Resume browsing or take over manually to continue.'
        # Canonical extraction and assistant text stay deterministic. Advisory
        # inference runs off the event loop and is persisted alongside findings.
        if self.local_provider is not None:
            structured['local_llm'] = await self.local_provider.enrich(items=items, request=user_request)
        else:
            structured['local_llm'] = {'status': 'disabled'}
        return structured, text

