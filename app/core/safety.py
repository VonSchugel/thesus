from __future__ import annotations

from urllib.parse import urlparse

from app.models.domain import PlannedAction

SENSITIVE_TERMS = {
    'submit',
    'send',
    'purchase',
    'checkout',
    'pay',
    'confirm_order',
    'place_order',
    'transfer',
}


def is_sensitive_action(action: PlannedAction, sensitive_domains: list[str]) -> bool:
    text = ' '.join([
        action.action_type.value if hasattr(action.action_type, 'value') else str(action.action_type),
        action.summary,
        ' '.join(f'{k}:{v}' for k, v in action.payload.items()),
    ]).lower()
    if any(term in text for term in SENSITIVE_TERMS):
        return True
    if action.page_url:
        host = (urlparse(action.page_url).hostname or '').lower()
        for domain in sensitive_domains:
            domain = domain.lower().strip()
            if not domain:
                continue
            if host == domain or host.endswith('.' + domain.lstrip('*.')):
                return True
    return False


def url_allowed(url: str, allowed_domains: list[str], prohibited_domains: list[str]) -> bool:
    if not url:
        return True
    host = (urlparse(url).hostname or '').lower()
    if allowed_domains:
        allowed = False
        for d in allowed_domains:
            d = d.lower().strip()
            if not d:
                continue
            needle = d.lstrip('*.')
            if host == needle or host.endswith('.' + needle):
                allowed = True
                break
        if not allowed:
            return False
    for d in prohibited_domains:
        d = d.lower().strip()
        if not d:
            continue
        needle = d.lstrip('*.')
        if host == needle or host.endswith('.' + needle):
            return False
    return True

