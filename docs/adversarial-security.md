# Adversarial security evidence

The experiments used temporary SQLite data and Chromium profiles on Windows 11 with Python 3.12.14, Playwright 1.64.1, and Chromium 145.0.7632.6. All target sites listened on loopback. The browser commands went through the live FastAPI workspace and real Chromium. Scripted planners made the exact action and fault timing repeatable. The untrusted-peer case supplies an explicit ASGI peer address, and the tunnel case uses two real TCP connections. No test used a real account or mutated external data.

```powershell
& $workspacePython -m pytest -q tests/test_adversarial_security.py
& $workspacePython -m pytest -q tests/test_browser_workspace.py -k "redirect_cannot or tabs_artifacts or approval or authentication"
```

## Reproduced defects and corrections

| Severity and boundary | Failing behavior and cause | Corrected behavior and verification |
| --- | --- | --- |
| **P0 when remotely bound. Host spoofing admitted an unauthenticated request.** | `POST /sessions` returned HTTP 200 to an ASGI peer at `192.0.2.41` with `Host: 127.0.0.1`. Host validation relied on caller-supplied text. Running the default loopback-bound launcher already restricts TCP exposure. | Without a workspace token, the application accepts loopback ASGI peers, including IPv4-mapped loopback. The same simulated remote peer now receives 403 and creates zero sessions. With a configured token, it receives 401 without credentials and 200 with a matching Bearer token. [Workspace middleware](../app/app.py), [regression cases](../tests/test_adversarial_security.py). |
| **P1. A native form changed while approval was pending.** | A button's element, label, method, and endpoint remained stable. JavaScript changed `amount=10` to `amount=9000` after the approval was presented. Chromium submitted `amount=9000`. Target metadata omitted the successful form controls. | The observation binds supported native form controls to the target in memory. Revalidation rejects a changed target or payload before dispatch. The real Chromium fixture receives zero requests after the pending mutation. Raw form values and payload hashes stay in memory. [Target binding](../app/integrations/browser/playwright_adapter.py), [regression cases](../tests/test_adversarial_security.py). |
| **P1. A click listener could rewrite the wire request after target revalidation.** | With only the target recheck in place, a synchronous `click` listener changed the same hidden amount. Chromium again sent `amount=9000`. The action boundary and the first request were distinct. | During a targeted native submit, the Chromium route admits one matching first navigation from the same main frame. Destination, method, and encoded query or body must match. Mismatched navigation and unsafe requests are aborted while the permit is active. The fixture receives zero requests for the tested click-time rewrite with either GET or POST. Unchanged GET and POST fixtures each receive exactly `amount=10&note=hello+%7E*%0D%0Anext` once. [Wire guard](../app/integrations/browser/playwright_adapter.py), [HTML form entry construction](https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#constructing-the-entry-list), [regression cases](../tests/test_adversarial_security.py). |
| **P2. Disconnected CONNECT clients retained proxy capacity.** | An authenticated client established a loopback tunnel and disconnected while the upstream was silent. The upstream had no EOF within the one-second test deadline. The proxy treated tunnel EOF like completion of an ordinary HTTP request body and could wait another five minutes for a response. These connections competed for 128 shared proxy slots. | Tunnel EOF now closes both relay directions. Ordinary HTTP still reads its response when the request body completes. The silent upstream sees EOF inside the one-second test bound. Native form submissions, private-origin redirect denial, and scoped file transfers still passed. [Proxy relay](../app/integrations/browser/egress_proxy.py), [CONNECT closure rules](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.3.6), [regression cases](../tests/test_adversarial_security.py). |

Eight corrected loopback tunnel trials measured a median of **0.223 ms** from client close to upstream EOF, ranging from **0.214 to 0.286 ms**. The original tunnel test supplies a lower bound of one second, since it stopped waiting at that point. A separate plain HTTP GET trial with a silent upstream still exceeded one second after the client disconnected. The ordinary HTTP response timeout still bounds that connection. The CONNECT improvement does not measure normal page latency or eliminate resource retention by abandoned HTTP requests. [Proxy relay](../app/integrations/browser/egress_proxy.py).

The wire guard intentionally covers simple, same-frame native URL-encoded forms. Unsupported cases include named targets, image submit controls, password and file fields, some form-associated custom controls, non-URL-encoded POSTs, and forms exceeding the observation bound. These cases require manual submission. The app compares the browser's request to a bounded snapshot of successful controls. This proves integrity for the tested changes, while the user still reviews the page and the method and destination shown in the approval. Browser page code and the destination server can interpret the action in ways the workspace cannot verify.

The synthetic login test started an agent task, paused for credentials, typed a password through the manual browser API, submitted once, and returned control in the same Chromium tab. The resumed agent saw the authenticated invoice through a fixture protected by an HttpOnly cookie. The synthetic password was absent from the inspected accessibility observation and the API's messages and event history. A website can still display or transmit information that the user submits to it. The experiment covers this login flow, not general secret redaction. [Synthetic login regression](../tests/test_adversarial_security.py).

The approval also has a separate persistence boundary. The recovery tests inject a failed SQLite write during approval rejection and after an actual form POST. A failed rejection preserves its pending proposal. A failed result write preserves uncertainty and requires review instead of repeating a browser effect. Requests already sent by Chromium retain their external effects after a database rollback. [Rollback and interrupted effect evidence](adversarial-persistence.md), [recovery tests](../tests/test_adversarial_recovery.py).

## Residual behavior reproduced in real Chromium

**P0 blocker for high-risk authenticated browsing.** Ordinary website code can issue its own requests with ambient browser credentials. The approval mechanism controls commands submitted by the workspace, including the bounded native-form path above. It cannot promise approval for every operation a site performs on page load, during manual use, or after a dispatched action. These are reproducible limits of the corrected version.

### Automatic request on page load

1. Serve this HTML on a temporary loopback origin and grant only that port in `BROWSER_PRIVATE_HOSTS`. On `GET /read`, respond with `Set-Cookie: audit=synthetic; HttpOnly; SameSite=Strict; Path=/`. Add a `POST /background-write` endpoint that records the body and the Cookie header.
2. Submit a read task with the explicit URL for `/read`. Use a scripted planner that opens that URL, then returns a read result. Do not issue any manual input or approval.

```html
<title>Read-only fixture</title>
<h1>Read this</h1>
<script>
  fetch('/background-write', {method: 'POST', body: 'renderer-only'})
</script>
```

The target recorded `/background-write`, body `renderer-only`, and cookie `audit=synthetic`. The task completed with zero `approval_requested` events. The normal host and IP checks allowed the fixture. Those checks answer where the browser can connect, while the renderer and the site determine what the request does.

### Method-preserving response redirect

1. Serve a simple self-targeted form with `method="post"`, `action="/first"`, one hidden input named `amount` with value `10`, and a named or labeled native submit button.
2. Record both `/first` and `/second` POSTs. Reply to `/first` with HTTP 307, `Location: /second`, and an empty response body.
3. Submit a task to open the form and click its submitter. Approve the resulting operation once and inspect the fixture requests.

The target recorded `POST /first` and `POST /second`, each with `amount=10`. The first request matched its permit. Chromium followed the redirect without another approval. Host policy still applies at the proxy, but a same-origin redirect passed it. [HTTP 307 semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-15.4.8), [Playwright redirect interception discussion](https://github.com/microsoft/playwright/issues/34994).

### Trade-offs of a broad HTTP method filter

A separate fixture loaded its visible data using `POST /graphql` with `{"query":"{ total }"}`. With the production routing policy the real page reached `Total: 46`. With a temporary Playwright route denying methods outside GET, HEAD, and OPTIONS it showed `Read failed`. Method-based blocking outside a manual command or form permit would close the demonstrated background POST and break this read workflow. A method filter alone cannot establish the meaning of a GET endpoint, cover all redirect chains, or classify messages after a WebSocket upgrade. [HTTP method semantics](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.2.1), [pinned Playwright WebSocket routing implementation](https://github.com/microsoft/playwright/blob/v1.64.1/packages/playwright-core/src/server/dispatchers/webSocketRouteDispatcher.ts).

For workflows requiring controlled external effects, the next meaningful boundary is a narrowly scoped, independently authorized application connector or an enforcing request gateway with an explicit endpoint policy and tests for redirects and long-lived channels. The current workspace should be evaluated as a supervised, single-principal browser for trusted workflows. Multi-tenant authorization requires a separate deployment boundary. A reverse proxy that presents all clients as loopback can also obscure the no-token peer boundary. Remote operation requires a workspace token and a trusted proxy configuration. Browser downloads can occupy Chromium's temporary storage before the workspace rejects an oversized artifact, so application artifact caps leave the need for disk isolation and quotas. [Workspace threat model](../ARCHITECTURE.md#threat-model).
