from __future__ import annotations

import httpx


class PinnedAddressTransport(httpx.AsyncBaseTransport):
    """Connect only to previously validated IPs, preserving HTTP and TLS identity."""

    def __init__(self, addresses: tuple[str, ...]) -> None:
        if not addresses:
            raise ValueError("At least one validated address is required.")
        self.addresses = addresses
        self.transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        # Keep the original Host header and certificate/SNI name. Sending the
        # original hostname to the network layer would allow a second DNS answer
        # to bypass the public-address check. Retry only connection failures.
        for index, address in enumerate(self.addresses):
            pinned = httpx.Request(
                method=request.method,
                url=request.url.copy_with(host=address),
                headers=request.headers,
                stream=request.stream,
                extensions={**request.extensions, "sni_hostname": request.url.host},
            )
            try:
                return await self.transport.handle_async_request(pinned)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if index == len(self.addresses) - 1:
                    raise
        raise AssertionError("Validated addresses must not be empty.")

    async def aclose(self) -> None:
        await self.transport.aclose()
