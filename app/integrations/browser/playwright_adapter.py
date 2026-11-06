from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
from dataclasses import dataclass, field
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from time import monotonic
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from PIL import Image

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.integrations.browser.egress_proxy import BrowserEgressProxy
from app.models.browser import (
    BrowserActionResult, BrowserArtifact, BrowserCapacity, BrowserCommand, BrowserElement, BrowserFrame,
    BrowserObservation, BrowserOutcomeUnknown, BrowserPolicyError, BrowserUnavailable, StaleBrowserState,
)
from app.models.domain import BrowserLiveState, SessionContext, SnapshotResult, TabState

if TYPE_CHECKING:
    from playwright.async_api import BrowserContext, CDPSession, Download, ElementHandle, Page, Playwright, Request, Route, WebSocketRoute

    from app.core.config import Settings


_TARGETS = ('a[href], button, input:not([type=hidden]), textarea, select, [contenteditable=true], '
            '[role=button], [role=link], [role=checkbox], [role=radio], [role=menuitem], [role=tab]')
_DESCRIBE_TARGET = """el => {
    const tag = el.tagName.toLowerCase();
    const inputType = tag === 'input' ? el.type : null;
    const role = el.getAttribute('role') || ({a: 'link', button: 'button', select: 'combobox',
        textarea: 'textbox', input: ({checkbox: 'checkbox', radio: 'radio', submit: 'button',
            button: 'button', file: 'button'})[inputType] || 'textbox'})[tag] || 'textbox';
    const label = el.getAttribute('aria-label') || [...(el.labels || [])].map(x => x.innerText).join(' ')
        || el.getAttribute('title') || el.getAttribute('placeholder') || el.getAttribute('alt')
        || (tag !== 'input' && tag !== 'textarea' && !el.isContentEditable ? el.innerText : '')
        || el.getAttribute('name') || role;
    const form = el.form || el.closest('form');
    const isSubmitter = tag === 'button' && el.type === 'submit' || tag === 'input' && ['submit', 'image'].includes(inputType);
    const method = form ? (el.hasAttribute('formmethod') ? el.formMethod : form.method) : null;
    function submission() {
        if (!form || !isSubmitter) return null;
        const target = (el.hasAttribute('formtarget') ? el.formTarget : form.target)
            || document.querySelector('base[target]')?.target || '_self';
        const encoding = el.hasAttribute('formenctype') ? el.formEnctype : form.enctype;
        if (target.toLowerCase() !== '_self' || !['get', 'post'].includes(method)
            || (method === 'post' && encoding !== 'application/x-www-form-urlencoded')) return false;
        // FormData construction can dispatch a page-controlled formdata event.
        // Assemble the successful controls for simple native forms without running it.
        let remaining = 65536;
        if (form.elements.length > 256 || inputType === 'image') return false;
        const entries = [];
        const normalize = text => text.replace(/\\r\\n|\\r|\\n/g, '\\r\\n');
        const charset = form.acceptCharset || document.characterSet;
        for (const field of form.elements) {
            const fieldTag = field.tagName.toLowerCase();
            if (fieldTag === 'fieldset' || fieldTag === 'output') continue;
            if (!['input', 'button', 'select', 'textarea'].includes(fieldTag)) return false;
            if (!field.name || field.matches(':disabled') || field.closest('datalist')) continue;
            const type = field.type;
            if (['button', 'reset'].includes(type) || (type === 'submit' && field !== el)
                || (['checkbox', 'radio'].includes(type) && !field.checked)) continue;
            if (['file', 'password'].includes(type) || field.name === '_charset_' || field.hasAttribute('dirname')
                || (fieldTag === 'textarea' && field.wrap === 'hard')) return false;
            if (fieldTag === 'select' && field.options.length > 256) return false;
            const values = fieldTag === 'select'
                ? Array.from(field.selectedOptions).filter(option => !option.matches(':disabled')).map(option => option.value)
                : [field.value];
            for (const value of values) {
                remaining -= field.name.length + value.length;
                if (remaining < 0 || (!/^utf-8$/i.test(charset) && /[^\\x00-\\x7f]/.test(field.name + value))) return false;
                entries.push([normalize(field.name), normalize(value)]);
            }
        }
        const body = new URLSearchParams(entries).toString();
        if (body.length > 65536) return false;
        return {body, state: JSON.stringify([encoding, target, el.formNoValidate, form.noValidate])};
    }
    return {
        tag, role, label: String(label).trim().replace(/\\s+/g, ' ').slice(0, 180),
        input_type: inputType, href: tag === 'a' && el.hasAttribute('href') ? el.href : null,
        form_action: form ? (el.hasAttribute('formaction') ? el.formAction : form.action) : null,
        form_method: method,
        form_submission: submission(),
        download: el.hasAttribute('download'),
        disabled: el.matches(':disabled,[aria-disabled=true]'),
        name: el.getAttribute('name'),
        visible: el.isConnected && !!el.getClientRects().length && getComputedStyle(el).visibility === 'visible',
    };
}"""
_COLLECT_TARGETS = f"""selector => {{
    const describe = {_DESCRIBE_TARGET};
    const nodes = [];
    const descriptions = [];
    for (const el of Array.prototype.slice.call(document.querySelectorAll(selector), 0, 300)) {{
        const details = describe(el);
        if (details.visible && !details.disabled) {{
            nodes.push(el);
            descriptions.push(details);
            if (nodes.length >= 40) break;
        }}
    }}
    return {{nodes, descriptions}};
}}"""
_SENSITIVE_QUERY = re.compile(r'^(?:code|token|access[_-]?token|refresh[_-]?token|id[_-]?token|'
                              r'api[_-]?key|secret|pass(?:word)?|session(?:id)?|signature|sig|auth|key|'
                              r'x-amz-.+|x-goog-.+)$', re.I)
_ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
_FRAME_CACHE_S = 0.20
_RETAINED_FRAMES = 4
_POINTER_RADIUS = 40
_MAX_ARTIFACTS = 32
_COMMAND_SUMMARIES = {
    'open': 'Navigated to the requested address', 'open_link': 'Opened the selected link',
    'back': 'Went back', 'forward': 'Went forward', 'reload': 'Reloaded the page',
    'new_tab': 'Opened a browser tab', 'switch_tab': 'Switched browser tabs', 'close_tab': 'Closed the browser tab',
    'click': 'Clicked in the browser', 'type': 'Entered text', 'key': 'Sent a key press', 'wheel': 'Scrolled the page',
    'resize': 'Resized the browser viewport', 'upload': 'Selected a staged file',
}


def public_url(url: str) -> str:
    """Keep routing information useful while stripping common URL credentials from UI and logs."""
    if url == 'about:blank':
        return url
    try:
        parts = urlsplit(url)
        if parts.scheme not in ('http', 'https'):
            return 'about:blank'
        query = parse_qsl(parts.query, keep_blank_values=True, max_num_fields=256)
        redacted_query = [(key, '[redacted]' if _SENSITIVE_QUERY.match(key) else value) for key, value in query]
        return urlunsplit((parts.scheme, parts.netloc.rsplit('@', 1)[-1], parts.path,
                           urlencode(redacted_query) if query != redacted_query else parts.query, ''))[:4096]
    except ValueError:
        return 'about:blank'


@dataclass
class _BoundTarget:
    handle: ElementHandle
    details: dict


@dataclass
class _NativeFormRequest:
    page: Page
    url: str
    method: str
    body_digest: str
    accepted: bool = False
    blocked: bool = False

    def matches(self, request: Request) -> bool:
        from playwright.async_api import Error

        if self.accepted or not request.is_navigation_request() or request.method != self.method:
            return False
        try:
            if request.frame != self.page.main_frame:
                return False
            actual, expected = urlsplit(request.url), urlsplit(self.url)
            if self.method == 'GET':
                body = actual.query.encode('ascii')
                actual = actual._replace(query='')
                expected = expected._replace(query='')
            else:
                content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
                if content_type != 'application/x-www-form-urlencoded':
                    return False
                length = request.headers.get('content-length')
                if length and int(length) > 65_536:
                    return False
                body = request.post_data_buffer or b''
        except (Error, ValueError, UnicodeError):
            return False
        return (len(body) <= 65_536 and actual._replace(fragment='') == expected._replace(fragment='')
                and sha256(body).hexdigest() == self.body_digest)


@dataclass
class _BrowserRuntime:
    context: BrowserContext
    viewport: dict[str, int]
    epoch: str = field(default_factory=lambda: uuid4().hex)
    generation: int = 0
    tabs: dict[str, Page] = field(default_factory=dict)
    active_tab_id: str = ''
    targets: dict[str, _BoundTarget] = field(default_factory=dict)
    targets_revision: str | None = None
    closed: bool = False
    cdp: CDPSession | None = None
    cdp_page: Page | None = None
    frame_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cached_frame: tuple[bytes, BrowserFrame] | None = None
    recent_frames: dict[str, tuple[bytes, BrowserFrame]] = field(default_factory=dict)
    captured_at: float = 0
    form_request: _NativeFormRequest | None = None

    @property
    def revision(self) -> str:
        return f'{self.epoch}-{self.generation}'

    def changed(self) -> None:
        self.generation += 1
        self.cached_frame = None
        self.recent_frames.clear()


class PlaywrightAdapter:
    """Own the Chromium context used by the workspace, planner observations, and human input."""

    def __init__(self, settings: Settings) -> None:
        if settings.browser_max_branches < 1:
            raise ValueError('BROWSER_MAX_BRANCHES must be at least 1')
        self.settings = settings
        self.profile_path_getter: Callable[[SessionContext], str] | None = None
        self._runtimes: dict[tuple[str, str], _BrowserRuntime] = {}
        self._playwright: Playwright | None = None
        self._proxy = BrowserEgressProxy(self._resolve_destination)
        self._lifecycle_lock = asyncio.Lock()
        self._capacity_changed = asyncio.Event()
        self._artifact_lock = asyncio.Lock()
        self._background: set[asyncio.Task] = set()

    def _profile(self, ctx: SessionContext) -> Path:
        if self.profile_path_getter is not None:
            return Path(self.profile_path_getter(ctx)).resolve()
        return (self.settings.profile_dir / ctx.session_id / ctx.branch_id).resolve()

    @staticmethod
    def _key(ctx: SessionContext) -> tuple[str, str]:
        return ctx.session_id, ctx.branch_id

    @staticmethod
    def _available(runtime: _BrowserRuntime | None) -> bool:
        return runtime is not None and not runtime.closed

    def _active(self, ctx: SessionContext) -> tuple[_BrowserRuntime, Page]:
        runtime = self._runtimes.get(self._key(ctx))
        if runtime is None or runtime.closed:
            raise BrowserUnavailable('Open or reconnect the browser first.')
        page = runtime.tabs.get(runtime.active_tab_id)
        if page is None or page.is_closed():
            raise BrowserUnavailable('The active browser tab is unavailable.')
        return runtime, page

    def _start_background(self, operation: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(operation)
        self._background.add(task)

        def done(finished: asyncio.Task) -> None:
            self._background.discard(finished)
            if not finished.cancelled():
                finished.exception()

        task.add_done_callback(done)

    async def start(self, session_ctx: SessionContext) -> None:
        await self.ensure_browser(session_ctx)

    def has_capacity(self, ctx: SessionContext) -> bool:
        return (self._available(self._runtimes.get(self._key(ctx))) or
                sum(not runtime.closed for runtime in self._runtimes.values()) < self.settings.browser_max_branches)

    async def wait_for_capacity(self, ctx: SessionContext) -> None:
        while not self.has_capacity(ctx):
            self._capacity_changed.clear()
            await self._capacity_changed.wait()

    async def ensure_browser(self, session_ctx: SessionContext) -> None:
        from playwright.async_api import Error, async_playwright

        key = self._key(session_ctx)
        async with self._lifecycle_lock:
            runtime = self._runtimes.get(key)
            if self._available(runtime):
                if not runtime.tabs:
                    await runtime.context.new_page()
                return
            if runtime is not None:
                self._runtimes.pop(key, None)
                try:
                    await runtime.context.close()
                except Error:
                    pass
            if not self.has_capacity(session_ctx):
                raise BrowserCapacity('Close an unused browser before opening another one.')
            self._runtimes = {item: runtime for item, runtime in self._runtimes.items() if not runtime.closed}
            profile = self._profile(session_ctx)
            profile.mkdir(parents=True, exist_ok=True)
            viewport = {'width': 1280, 'height': 800}
            try:
                if self._playwright is None:
                    self._playwright = await async_playwright().start()
                context = await self._playwright.chromium.launch_persistent_context(
                    user_data_dir=profile / 'chromium', headless=True, chromium_sandbox=self.settings.browser_sandbox,
                    executable_path=self.settings.browser_executable_path, viewport=viewport, device_scale_factor=1,
                    accept_downloads=True, service_workers='block', proxy=await self._proxy.start(),
                    args=['--disable-quic', '--force-webrtc-ip-handling-policy=disable_non_proxied_udp'],
                    timeout=max(15000, self.settings.browser_timeout_ms),
                )
            except Error:
                raise BrowserUnavailable('Chromium could not start. Check the installation and profile directory.') from None
            runtime = _BrowserRuntime(context=context, viewport=viewport)
            self._runtimes[key] = runtime
            try:
                context.set_default_timeout(self.settings.browser_timeout_ms)
                context.set_default_navigation_timeout(self.settings.browser_timeout_ms)
                def closed(_) -> None:
                    runtime.closed = True
                    self._capacity_changed.set()

                context.on('close', closed)
                async def route_request(route: Route) -> None:
                    await self._route(route, runtime)

                await context.route('**/*', route_request)
                await context.route_web_socket('**/*', self._route_websocket)
                context.on('page', lambda page: self._register_page(session_ctx, runtime, page))
                for page in context.pages:
                    self._register_page(session_ctx, runtime, page)
                if not runtime.tabs:
                    await context.new_page()
            except BaseException:
                self._runtimes.pop(key, None)
                self._capacity_changed.set()
                await context.close()
                raise

    def _register_page(self, ctx: SessionContext, runtime: _BrowserRuntime, page: Page) -> None:
        if len(runtime.tabs) >= self.settings.browser_max_tabs:
            self._start_background(page.close())
            return
        tab_id = uuid4().hex
        runtime.tabs[tab_id] = page
        runtime.active_tab_id = tab_id
        runtime.changed()

        def remove(_: Page) -> None:
            runtime.tabs.pop(tab_id, None)
            if runtime.active_tab_id == tab_id:
                runtime.active_tab_id = next(reversed(runtime.tabs), '')
            runtime.changed()

        def navigated(frame) -> None:
            if frame == page.main_frame:
                runtime.changed()

        def crashed(_: Page) -> None:
            remove(page)
            self._start_background(page.close())

        page.on('close', remove)
        page.on('crash', crashed)
        page.on('framenavigated', navigated)
        page.on('download', lambda download: self._start_background(self._receive_download(ctx, download)))

    async def close(self, session_ctx: SessionContext) -> None:
        async with self._lifecycle_lock:
            runtime = self._runtimes.pop(self._key(session_ctx), None)
            if runtime is not None:
                try:
                    await runtime.context.close()
                finally:
                    self._capacity_changed.set()

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            runtimes = list(self._runtimes.values())
            self._runtimes.clear()
            await asyncio.gather(*(runtime.context.close() for runtime in runtimes), return_exceptions=True)
            for task in list(self._background):
                task.cancel()
            await asyncio.gather(*list(self._background), return_exceptions=True)
            if self._playwright is not None:
                await self._playwright.stop()
                self._playwright = None
            await self._proxy.close()

    @staticmethod
    def _host_matches(host: str, domains: list[str]) -> bool:
        return any(host == domain or (domain.startswith('*.') and host.endswith(domain[1:])) for domain in domains)

    async def _check_url(self, url: str, *, websocket: bool = False) -> None:
        if url == 'about:blank' and not websocket:
            return
        await self._resolve_destination(url, websocket=websocket)

    async def _resolve_destination(self, url: str, *, websocket: bool = False) -> tuple[list[str], int]:
        try:
            parts = urlsplit(url)
            host = (parts.hostname or '').lower().rstrip('.').encode('idna').decode('ascii')
            port = parts.port if parts.port is not None else (443 if parts.scheme in ('https', 'wss') else 80)
        except (UnicodeError, ValueError) as exc:
            raise BrowserPolicyError('The browser address is invalid.') from exc
        if (not host or not port or '%' in host or '\\' in url or any(ord(char) < 32 or ord(char) == 127 for char in url)
                or parts.scheme not in (('ws', 'wss') if websocket else ('http', 'https'))
                or parts.username is not None or parts.password is not None):
            raise BrowserPolicyError('Use an HTTP or HTTPS address without embedded credentials.')
        allowed = [domain.lower().rstrip('.') for domain in self.settings.browser_allowed_domains]
        prohibited = [domain.lower().rstrip('.') for domain in self.settings.browser_prohibited_domains]
        if self._host_matches(host, prohibited) or (allowed and not self._host_matches(host, allowed)):
            raise BrowserPolicyError('The browser domain is outside the configured access policy.')
        authority = f'[{host}]:{port}' if ':' in host else f'{host}:{port}'
        private_hosts = {host_spec.lower() for host_spec in self.settings.browser_private_hosts}
        allow_private = host in private_hosts or authority in private_hosts
        try:
            addresses = [ipaddress.ip_address(host)]
        except ValueError:
            try:
                loop = asyncio.get_running_loop()
                results = await asyncio.wait_for(loop.getaddrinfo(host, port, type=socket.SOCK_STREAM), 2)
                addresses = [ipaddress.ip_address(result[4][0]) for result in results]
            except (OSError, ValueError, TimeoutError) as exc:
                raise BrowserPolicyError('The browser destination could not be verified.') from exc
        if not addresses or (not allow_private and any(not address.is_global or address.is_multicast for address in addresses)):
            raise BrowserPolicyError('Private browser destinations require explicit configuration.')
        return list(dict.fromkeys(str(address) for address in addresses)), port

    async def _route(self, route: Route, runtime: _BrowserRuntime) -> None:
        request = route.request
        permit = runtime.form_request
        try:
            await self._check_url(request.url)
        except BrowserPolicyError:
            await route.abort('blockedbyclient')
            return
        if permit and (request.is_navigation_request() or request.method not in {'GET', 'HEAD', 'OPTIONS'}):
            if not permit.matches(request):
                permit.blocked = True
                await route.abort('blockedbyclient')
                return
            permit.accepted = True
        await route.continue_()

    async def _route_websocket(self, route: WebSocketRoute) -> None:
        try:
            await self._check_url(route.url, websocket=True)
        except BrowserPolicyError:
            await route.close(code=1008, reason='Browser access policy')
        else:
            route.connect_to_server()

    async def restore(self, ctx: SessionContext, live: BrowserLiveState) -> None:
        await self.ensure_browser(ctx)
        _, page = self._active(ctx)
        if page.url == 'about:blank' and live.url and live.url != 'about:blank' and '%5Bredacted%5D' not in live.url:
            try:
                await self.execute(ctx, BrowserCommand(kind='open', url=live.url))
            except (BrowserUnavailable, BrowserPolicyError):
                return

    async def pause(self, session_ctx: SessionContext) -> None:
        # The orchestrator owns execution and admission to this adapter.
        pass

    async def resume(self, session_ctx: SessionContext) -> None:
        pass

    @staticmethod
    async def _title(page: Page) -> str:
        from playwright.async_api import Error

        try:
            return (await asyncio.wait_for(page.title(), timeout=1))[:400]
        except (Error, TimeoutError):
            return ''

    async def get_state(self, session_ctx: SessionContext) -> BrowserLiveState:
        try:
            runtime, page = self._active(session_ctx)
        except BrowserUnavailable:
            return BrowserLiveState(artifacts=[item.model_dump() for item in self.list_artifacts(session_ctx)])
        tab_id, revision = runtime.active_tab_id, runtime.revision
        pages = list(runtime.tabs.items())
        titles = await asyncio.gather(*(self._title(tab) for _, tab in pages))
        tabs = [TabState(tab_id=key, url=public_url(tab.url), title=title)
                for (key, tab), title in zip(pages, titles) if not tab.is_closed()]
        size = page.viewport_size or runtime.viewport
        return BrowserLiveState(
            url=public_url(page.url), title=next((tab.title for tab in tabs if tab.tab_id == tab_id), ''),
            tabs=tabs, active_tab_id=tab_id, revision=revision, width=size['width'], height=size['height'],
            artifacts=[item.model_dump() for item in self.list_artifacts(session_ctx)],
        )

    async def capture_snapshot(self, session_ctx: SessionContext) -> SnapshotResult:
        return SnapshotResult(live_state=await self.get_state(session_ctx), metadata={'adapter': 'playwright'})

    async def frame(self, ctx: SessionContext) -> tuple[bytes, BrowserFrame]:
        from playwright.async_api import Error

        try:
            runtime, _ = self._active(ctx)
        except BrowserUnavailable as exc:
            raise ConflictError('The browser viewport is not open.') from exc
        async with runtime.frame_lock:
            current, page = self._active(ctx)
            if current is not runtime:
                raise StaleBrowserState('The browser was reconnected. Request a new frame.')
            if runtime.cached_frame is not None and monotonic() - runtime.captured_at < _FRAME_CACHE_S:
                return runtime.cached_frame
            tab_id, revision = runtime.active_tab_id, runtime.revision
            size = page.viewport_size or runtime.viewport
            try:
                jpeg = await self._capture_jpeg(page)
            except (Error, TimeoutError):
                raise BrowserUnavailable('The browser could not capture a frame.') from None
            if runtime.closed or runtime.revision != revision:
                raise StaleBrowserState('The viewport changed while capturing a frame.')
            frame = BrowserFrame(tab_id=tab_id, revision=revision, frame_id=uuid4().hex,
                                 width=size['width'], height=size['height'])
            runtime.cached_frame = (jpeg, frame)
            runtime.recent_frames[frame.frame_id] = runtime.cached_frame
            if len(runtime.recent_frames) > _RETAINED_FRAMES:
                del runtime.recent_frames[next(iter(runtime.recent_frames))]
            runtime.captured_at = monotonic()
            return jpeg, frame

    @staticmethod
    async def _capture_jpeg(page: Page) -> bytes:
        return await asyncio.wait_for(page.screenshot(
            type='jpeg', quality=70, timeout=1500, scale='css',
            mask=[page.locator('input[type=password]')], mask_color='#242b38',
        ), timeout=2)

    @staticmethod
    def _same_pointer_area(earlier: bytes, current: bytes, x: int, y: int) -> bool:
        if earlier == current:
            return True
        with Image.open(BytesIO(earlier)) as previous, Image.open(BytesIO(current)) as fresh:
            if previous.size != fresh.size:
                return False
            area = (max(0, x - _POINTER_RADIUS), max(0, y - _POINTER_RADIUS),
                    min(previous.width, x + _POINTER_RADIUS), min(previous.height, y + _POINTER_RADIUS))
            return previous.crop(area).tobytes() == fresh.crop(area).tobytes()

    async def _verify_pointer_frame(self, runtime: _BrowserRuntime, page: Page, command: BrowserCommand,
                                    frame_id: str) -> None:
        from playwright.async_api import Error

        if command.kind != 'click' or command.x is None or command.y is None:
            raise ValidationError('A frame ID can only accompany a coordinate click.')
        self._check_coordinates(page, runtime, command)
        async with runtime.frame_lock:
            reference = runtime.recent_frames.get(frame_id)
            if reference is None or reference[1].revision != runtime.revision or reference[1].tab_id != runtime.active_tab_id:
                raise StaleBrowserState('The displayed frame expired. Try again on the latest frame.')
            try:
                fresh = await self._capture_jpeg(page)
                same = await asyncio.to_thread(self._same_pointer_area, reference[0], fresh, int(command.x), int(command.y))
            except (Error, OSError, TimeoutError):
                raise StaleBrowserState('The displayed frame could not be rechecked. Try again on the latest frame.') from None
            if not same:
                raise StaleBrowserState('The page changed near the pointer. Try again on the latest frame.')

    async def _accessibility_tree(self, runtime: _BrowserRuntime, page: Page) -> str:
        from playwright.async_api import Error

        if runtime.cdp_page != page:
            if runtime.cdp is not None:
                try:
                    await runtime.cdp.detach()
                except Error:
                    pass
            runtime.cdp = await runtime.context.new_cdp_session(page)
            runtime.cdp_page = page
        result = await runtime.cdp.send('Accessibility.getFullAXTree', {'depth': 6})
        nodes = result.get('nodes', [])
        control_roles = ('textbox', 'searchbox', 'combobox', 'spinbutton')
        parents = {node.get('nodeId'): node.get('parentId') for node in nodes}
        editables = {node.get('nodeId') for node in nodes if node.get('role', {}).get('value') in control_roles
                     or any(prop.get('name') == 'editable' for prop in node.get('properties', []))}
        lines = []
        for node in nodes:
            role = node.get('role', {}).get('value', '')
            if node.get('ignored') or role in ('InlineTextBox', 'generic', 'none'):
                continue
            name = str(node.get('name', {}).get('value', ''))
            # Text controls may contribute internal nodes carrying their live values.
            if node.get('nodeId') in editables:
                name = ''
            parent = node.get('parentId')
            while parent is not None and parent not in editables:
                parent = parents.get(parent)
            if parent in editables:
                continue
            lines.append(f'{role} {" ".join(name.split())[:160]}'.rstrip())
            if len(lines) >= 70:
                break
        return '\n'.join(lines)[:6000]

    async def _capture_targets(self, runtime: _BrowserRuntime, page: Page, revision: str) -> list[BrowserElement]:
        captured = await page.evaluate_handle(_COLLECT_TARGETS, _TARGETS)
        descriptions_handle = await captured.get_property('descriptions')
        nodes = await captured.get_property('nodes')
        try:
            descriptions = await descriptions_handle.json_value()
            handles = await nodes.get_properties()
        finally:
            await asyncio.gather(captured.dispose(), descriptions_handle.dispose(), nodes.dispose(), return_exceptions=True)
        new_targets: dict[str, _BoundTarget] = {}
        elements = []
        for index, details in enumerate(descriptions):
            handle = handles[str(index)].as_element()
            if handle is None:
                continue
            target_id = uuid4().hex[:12]
            new_targets[target_id] = _BoundTarget(handle=handle, details=self._bound_details(details))
            elements.append(BrowserElement(
                target_id=target_id, role=details['role'], label=details['label'], tag=details['tag'],
                input_type=details['input_type'], href=public_url(details['href']) if details['href'] else None,
                form_action=public_url(details['form_action']) if details['form_action'] else None,
                form_method=details['form_method'],
            ))
        if runtime.revision != revision:
            await self._dispose_targets(new_targets)
            raise StaleBrowserState('The page changed during observation.')
        await self._dispose_targets(runtime.targets)
        runtime.targets = new_targets
        runtime.targets_revision = revision
        return elements

    @staticmethod
    async def _dispose_targets(targets: dict[str, _BoundTarget]) -> None:
        await asyncio.gather(*(target.handle.dispose() for target in targets.values()), return_exceptions=True)

    @staticmethod
    def _bound_details(details: dict) -> dict:
        submission = details.get('form_submission')
        if isinstance(submission, dict):
            details['form_submission'] = {'state': submission['state'],
                                          'digest': sha256(submission['body'].encode('ascii')).hexdigest()}
        return details

    async def observe(self, ctx: SessionContext, *, visual: bool = False) -> BrowserObservation:
        from playwright.async_api import Error

        runtime, page = self._active(ctx)
        revision, tab_id = runtime.revision, runtime.active_tab_id
        try:
            ax_tree = await asyncio.wait_for(self._accessibility_tree(runtime, page), timeout=2)
            elements = await asyncio.wait_for(self._capture_targets(runtime, page, revision), timeout=2)
            screenshot = (await self.frame(ctx))[0] if visual else None
        except (Error, TimeoutError):
            raise BrowserUnavailable('The browser page could not be observed. Refresh its current state.') from None
        title = await self._title(page)
        if runtime.closed or runtime.revision != revision:
            raise StaleBrowserState('The page changed during observation.')
        return BrowserObservation(revision=revision, tab_id=tab_id, url=public_url(page.url), title=title,
                                  ax_tree=ax_tree, elements=elements, screenshot=screenshot)

    async def _target(self, runtime: _BrowserRuntime, target_id: str) -> _BoundTarget:
        from playwright.async_api import Error

        target = runtime.targets.get(target_id) if runtime.targets_revision == runtime.revision else None
        if target is None:
            raise StaleBrowserState('The browser target expired. Observe the page again.')
        try:
            details = self._bound_details(await target.handle.evaluate(_DESCRIBE_TARGET))
        except Error:
            raise StaleBrowserState('The browser target is gone. Observe the page again.') from None
        if details != target.details:
            raise StaleBrowserState('The browser target changed. Observe the page again.')
        if details['form_submission'] is False:
            raise BrowserPolicyError('This form requires direct manual submission.')
        return target

    async def execute(self, ctx: SessionContext, command: BrowserCommand, *,
                      expected_revision: str | None = None, frame_id: str | None = None) -> BrowserActionResult:
        from playwright.async_api import Error, TimeoutError as PlaywrightTimeout

        runtime, page = self._active(ctx)
        revision = runtime.revision
        if expected_revision is not None and revision != expected_revision:
            raise StaleBrowserState('The browser changed. Refresh its state before acting.')
        try:
            target = (await asyncio.wait_for(self._target(runtime, command.target_id), 2)
                      if command.target_id else None)
            url = command.url
            if command.kind == 'open_link':
                if target is None or target.details['tag'] != 'a' or target.details['download'] or not target.details['href']:
                    raise BrowserPolicyError('Choose a regular navigable link from the current page.')
                url = target.details['href']
            if url is not None:
                await self._check_url(url)
            if frame_id is not None:
                await self._verify_pointer_frame(runtime, page, command, frame_id)
            if runtime.closed or runtime.revision != revision:
                raise StaleBrowserState('The browser changed while preparing the action.')
        except (TimeoutError, PlaywrightTimeout):
            raise StaleBrowserState('The target could not be rechecked before the action.') from None
        runtime.changed()
        try:
            async with asyncio.timeout(self.settings.browser_timeout_ms / 1000 + 1):
                await self._dispatch(ctx, runtime, page, command, target, url)
            runtime, active = self._active(ctx)
        except (TimeoutError, PlaywrightTimeout, BrowserUnavailable):
            raise BrowserOutcomeUnknown('The browser did not confirm the action. Inspect the page before continuing.') from None
        except Error as exc:
            if 'ERR_BLOCKED_BY_CLIENT' in str(exc) or 'ERR_BLOCKED_BY_ADMINISTRATOR' in str(exc):
                raise BrowserPolicyError('The browser blocked the destination.') from None
            raise BrowserOutcomeUnknown('The browser did not confirm the action. Inspect the page before continuing.') from None
        return BrowserActionResult(summary=_COMMAND_SUMMARIES[command.kind], url=public_url(active.url),
                                   revision=runtime.revision, tab_id=runtime.active_tab_id,
                                   artifacts=self.list_artifacts(ctx))

    async def _dispatch(self, ctx: SessionContext, runtime: _BrowserRuntime, page: Page, command: BrowserCommand,
                        target: _BoundTarget | None, url: str | None) -> None:
        kind = command.kind
        if kind in ('open', 'open_link'):
            await self._check_navigation(await page.goto(url, wait_until='domcontentloaded'))
        elif kind == 'back':
            await self._check_navigation(await page.go_back(wait_until='domcontentloaded'))
        elif kind == 'forward':
            await self._check_navigation(await page.go_forward(wait_until='domcontentloaded'))
        elif kind == 'reload':
            await self._check_navigation(await page.reload(wait_until='domcontentloaded'))
        elif kind == 'new_tab':
            if len(runtime.tabs) >= self.settings.browser_max_tabs:
                raise ConflictError('Close a tab before opening another one.')
            new_page = await runtime.context.new_page()
            if url is not None:
                await self._check_navigation(await new_page.goto(url, wait_until='domcontentloaded'))
        elif kind in ('switch_tab', 'close_tab'):
            tab_id = command.tab_id or runtime.active_tab_id
            tab = runtime.tabs.get(tab_id)
            if tab is None or tab.is_closed():
                raise StaleBrowserState('That browser tab is no longer open.')
            if kind == 'switch_tab':
                runtime.active_tab_id = tab_id
                await tab.bring_to_front()
            else:
                await tab.close()
                if not runtime.tabs:
                    await runtime.context.new_page()
        elif kind == 'click' and target is not None:
            await self._click_bound_target(runtime, page, target, command.button)
        elif kind == 'click':
            self._check_coordinates(page, runtime, command)
            await page.mouse.click(command.x, command.y, button=command.button)
        elif kind == 'type' and target is not None:
            await target.handle.fill(command.text)
        elif kind == 'type':
            await page.keyboard.insert_text(command.text)
        elif kind == 'key':
            await page.keyboard.press(command.key)
        elif kind == 'wheel':
            if command.x is not None:
                self._check_coordinates(page, runtime, command)
                await page.mouse.move(command.x, command.y)
            await page.mouse.wheel(command.delta_x, command.delta_y)
        elif kind == 'resize':
            await page.set_viewport_size({'width': command.width, 'height': command.height})
        elif kind == 'upload':
            path, artifact = self.artifact_path(ctx, command.artifact_id)
            if artifact.kind != 'upload':
                raise BrowserPolicyError('Choose a staged upload.')
            payload = {'name': artifact.name, 'mimeType': 'application/octet-stream',
                       'buffer': await asyncio.to_thread(path.read_bytes)}
            if target is not None:
                await target.handle.set_input_files(payload)
            else:
                await page.locator('input[type=file]').set_input_files(payload)

    @staticmethod
    async def _check_navigation(response) -> None:
        if response is not None and response.status == 403 and await response.header_value('X-Thesus-Browser-Policy') == 'blocked':
            raise BrowserPolicyError('The browser blocked the destination.')

    @staticmethod
    async def _click_bound_target(runtime: _BrowserRuntime, page: Page, target: _BoundTarget, button: str) -> None:
        submission = target.details['form_submission']
        if submission is None:
            await target.handle.click(button=button)
            return
        permit = _NativeFormRequest(page, target.details['form_action'], target.details['form_method'].upper(), submission['digest'])
        runtime.form_request = permit
        try:
            await target.handle.click(button=button)
        finally:
            runtime.form_request = None
        if permit.blocked:
            raise BrowserPolicyError('The submitted form request differed from the approved form.')

    @staticmethod
    def _check_coordinates(page: Page, runtime: _BrowserRuntime, command: BrowserCommand) -> None:
        size = page.viewport_size or runtime.viewport
        if command.x >= size['width'] or command.y >= size['height']:
            raise ValidationError('Browser coordinates are outside the current viewport.')

    def _artifacts_dir(self, ctx: SessionContext) -> Path:
        return self._profile(ctx) / 'artifacts'

    @staticmethod
    def _safe_filename(filename: str) -> str:
        name = re.split(r'[/\\]', filename)[-1]
        name = ''.join(char for char in name if ord(char) >= 32 and ord(char) != 127).strip(' .')
        return name[:160] or 'file'

    def artifact_path(self, ctx: SessionContext, artifact_id: str) -> tuple[Path, BrowserArtifact]:
        if not _ID_PATTERN.fullmatch(artifact_id):
            raise NotFoundError('Browser artifact was not found.')
        root = self._artifacts_dir(ctx)
        data = root / f'{artifact_id}.data'
        metadata = root / f'{artifact_id}.json'
        try:
            artifact = BrowserArtifact.model_validate_json(metadata.read_text(encoding='utf-8'))
            if artifact.id == artifact_id and data.is_file():
                return data, artifact
        except (OSError, ValueError):
            pass
        raise NotFoundError('Browser artifact was not found.')

    def list_artifacts(self, ctx: SessionContext) -> list[BrowserArtifact]:
        items = []
        for entry in self._artifacts_dir(ctx).glob('*.json'):
            try:
                _, artifact = self.artifact_path(ctx, entry.stem)
            except NotFoundError:
                continue
            items.append(artifact)
        return items

    async def store_upload(self, ctx: SessionContext, filename: str, content: bytes) -> BrowserArtifact:
        return await self._store_artifact(ctx, filename, content, 'upload')

    async def _store_artifact(self, ctx: SessionContext, filename: str, content: bytes, kind) -> BrowserArtifact:
        if len(content) > self.settings.browser_artifact_limit_bytes:
            raise ValidationError('The browser artifact exceeds the configured size limit.', status_code=413)
        async with self._artifact_lock:
            root = self._artifacts_dir(ctx)
            if len(list(root.glob('*.data'))) >= _MAX_ARTIFACTS:
                raise ConflictError('The browser branch has reached its artifact limit.')
            artifact = BrowserArtifact(id=uuid4().hex, name=self._safe_filename(filename), kind=kind, size=len(content))
            await asyncio.to_thread(self._write_artifact, root, artifact, content)
            return artifact

    @staticmethod
    def _write_artifact(root: Path, artifact: BrowserArtifact, content: bytes) -> None:
        root.mkdir(parents=True, exist_ok=True)
        data = root / f'{artifact.id}.data'
        metadata = root / f'{artifact.id}.json'
        pending = metadata.with_suffix('.pending')
        try:
            data.write_bytes(content)
            pending.write_text(artifact.model_dump_json(), encoding='utf-8')
            pending.replace(metadata)
        except OSError:
            data.unlink(missing_ok=True)
            pending.unlink(missing_ok=True)
            raise

    async def _receive_download(self, ctx: SessionContext, download: Download) -> None:
        from playwright.async_api import Error

        try:
            path = await asyncio.wait_for(download.path(), timeout=20)
            with Path(path).open('rb') as source:
                content = source.read(self.settings.browser_artifact_limit_bytes + 1)
            await self._store_artifact(ctx, download.suggested_filename, content, 'download')
        except (Error, OSError, TimeoutError, ValidationError, ConflictError):
            pass
        finally:
            try:
                await asyncio.wait_for(download.cancel(), timeout=2)
                await asyncio.wait_for(download.delete(), timeout=2)
            except (Error, TimeoutError):
                pass
