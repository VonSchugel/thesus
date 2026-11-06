from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from contextlib import suppress
from secrets import compare_digest, token_urlsafe
from urllib.parse import urlsplit, urlunsplit

import h11

from app.models.browser import BrowserPolicyError


class BrowserEgressProxy:
    """Require an approved, pinned address for each Chromium HTTP request or TLS tunnel."""

    def __init__(self, resolve: Callable[[str], Awaitable[tuple[list[str], int]]]) -> None:
        self._resolve = resolve
        self._server: asyncio.Server | None = None
        self._clients: set[asyncio.Task] = set()
        self._password = token_urlsafe(24)
        self._authorization = b'Basic ' + base64.b64encode(f'browser:{self._password}'.encode())

    async def start(self) -> dict[str, str]:
        if self._server is None:
            self._server = await asyncio.start_server(self._accept, '127.0.0.1', 0)
        port = self._server.sockets[0].getsockname()[1]
        return {'server': f'http://127.0.0.1:{port}', 'username': 'browser', 'password': self._password,
                'bypass': '<-loopback>'}

    async def close(self) -> None:
        server = self._server
        if server is not None:
            server.close()
        tasks = list(self._clients)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            # Windows can leave a just-reset client transport attached during shutdown.
            with suppress(TimeoutError):
                await asyncio.wait_for(server.wait_closed(), timeout=2)
            self._server = None

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if len(self._clients) >= 128:
            writer.close()
            return
        self._clients.add(task)
        upstream_writer = None
        response_started = False
        try:
            connection = h11.Connection(h11.SERVER, max_incomplete_event_size=32_768)
            request = await asyncio.wait_for(self._read_event(connection, reader), timeout=8)
            if not isinstance(request, h11.Request):
                return
            auth = dict(request.headers).get(b'proxy-authorization', b'')
            if not compare_digest(auth, self._authorization):
                writer.write(b'HTTP/1.1 407 Proxy Authentication Required\r\n'
                             b'Proxy-Authenticate: Basic realm="Browser workspace"\r\n'
                             b'Content-Length: 0\r\nConnection: close\r\n\r\n')
                await writer.drain()
                return

            target = request.target.decode('ascii')
            if request.method == b'CONNECT':
                if any(char in target for char in '/?#'):
                    raise BrowserPolicyError('Invalid browser tunnel destination.')
                addresses, port = await self._resolve(f'https://{target}/')
                upstream_reader, upstream_writer = await self._connect(addresses, port)
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                response_started = True
                trailing, _ = connection.trailing_data
                upstream_writer.write(trailing)
                await self._relay(self._copy(reader, upstream_writer), self._copy(upstream_reader, writer),
                                  finish_response=False)
            else:
                parts = urlsplit(target)
                if parts.scheme != 'http':
                    raise BrowserPolicyError('Invalid browser proxy request.')
                addresses, port = await self._resolve(target)
                upstream_reader, upstream_writer = await self._connect(addresses, port)
                response_started = True
                await self._relay(self._http_request(connection, reader, upstream_writer, request, parts),
                                  self._copy(upstream_reader, writer), finish_response=True)
        except BrowserPolicyError:
            if not response_started:
                writer.write(b'HTTP/1.1 403 Forbidden\r\nX-Thesus-Browser-Policy: blocked\r\n'
                             b'Content-Length: 0\r\nConnection: close\r\n\r\n')
                with suppress(OSError):
                    await writer.drain()
        except (h11.ProtocolError, OSError, ValueError, TimeoutError):
            if not response_started:
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
                with suppress(OSError):
                    await writer.drain()
        finally:
            if upstream_writer is not None:
                upstream_writer.close()
            writer.close()
            self._clients.discard(task)

    @staticmethod
    async def _connect(addresses: list[str], port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        for address in addresses[:8]:
            try:
                return await asyncio.wait_for(asyncio.open_connection(address, port), timeout=2)
            except (OSError, TimeoutError):
                continue
        raise OSError('Browser origin connection failed.')

    @staticmethod
    async def _read_event(connection: h11.Connection, reader: asyncio.StreamReader):
        event = connection.next_event()
        while event is h11.NEED_DATA:
            connection.receive_data(await reader.read(16_384))
            event = connection.next_event()
        return event

    async def _http_request(self, source: h11.Connection, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                            request: h11.Request, parts) -> None:
        connection = h11.Connection(h11.CLIENT)
        connection_tokens = {token.strip().lower() for name, value in request.headers if name == b'connection'
                             for token in value.split(b',')}
        ignored = {b'host', b'connection', b'keep-alive', b'proxy-authorization', b'proxy-connection', b'upgrade'}
        headers = [(key, value) for key, value in request.headers if key not in ignored | connection_tokens]
        headers += [(b'host', parts.netloc.encode('ascii')), (b'connection', b'close')]
        target = urlunsplit(('', '', parts.path or '/', parts.query, '')).encode('ascii')
        writer.write(connection.send(h11.Request(method=request.method, target=target, headers=headers)))
        await writer.drain()
        while True:
            event = await self._read_event(source, reader)
            if not isinstance(event, (h11.Data, h11.EndOfMessage)):
                return
            writer.write(connection.send(event))
            await writer.drain()
            if isinstance(event, h11.EndOfMessage):
                return

    @staticmethod
    async def _copy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        while data := await reader.read(65_536):
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=15)

    @staticmethod
    async def _relay(outbound, inbound, *, finish_response: bool) -> None:
        request = asyncio.create_task(outbound)
        response = asyncio.create_task(inbound)
        try:
            async with asyncio.timeout(300):
                done, _ = await asyncio.wait((request, response), return_when=asyncio.FIRST_COMPLETED)
                # Finishing an HTTP request body leaves a response to read.
                # Reaching EOF in a CONNECT tunnel closes both directions.
                if finish_response and request in done and response not in done and request.exception() is None:
                    await response
        finally:
            for task in (request, response):
                task.cancel()
            await asyncio.gather(request, response, return_exceptions=True)
