import httpx
import pytest

from app.business.security_scan.http_transport import PinnedAddressTransport


@pytest.mark.anyio
async def test_pinned_transport_retries_only_validated_ips_and_keeps_tls_identity(monkeypatch):
    requests = []
    options = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("unreachable", request=request)
        return httpx.Response(200, content=b"ok")

    def transport(**kwargs):
        options.append(kwargs)
        return httpx.MockTransport(handle)

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    async with httpx.AsyncClient(transport=PinnedAddressTransport(("93.184.216.34", "2606:4700:4700::1111")), trust_env=False) as client:
        response = await client.get("https://example.com:8443/path?q=1")
    assert response.status_code == 200
    assert [r.url.host for r in requests] == ["93.184.216.34", "2606:4700:4700::1111"]
    assert all(r.headers["host"] == "example.com:8443" for r in requests)
    assert all(r.extensions["sni_hostname"] == "example.com" for r in requests)
    assert options == [{"trust_env": False, "retries": 0}]


@pytest.mark.anyio
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout])
async def test_pinned_transport_fails_closed_without_hostname_fallback(monkeypatch, failure):
    requests = []
    def handle(request):
        requests.append(request)
        raise failure("network failed", request=request)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handle))
    async with httpx.AsyncClient(transport=PinnedAddressTransport(("93.184.216.34", "1.1.1.1")), trust_env=False) as client:
        with pytest.raises(failure):
            await client.get("https://example.com/")
    assert len(requests) == (1 if failure is httpx.ReadTimeout else 2)
    assert all(r.url.host != "example.com" for r in requests)


@pytest.mark.anyio
async def test_real_httpx_transport_connects_to_ip_but_checks_original_tls_name():
    """Exercise HTTPX/HTTPCore connection setup with an in-memory network stream."""
    import ssl

    connected = []
    tls = []
    written = []

    class Stream:
        response = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok"

        async def read(self, max_bytes, timeout=None):
            value, self.response = self.response, b""
            return value

        async def write(self, buffer, timeout=None):
            written.append(buffer)

        async def aclose(self):
            pass

        async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
            tls.append((server_hostname, ssl_context.verify_mode, ssl_context.check_hostname))
            return self

        def get_extra_info(self, info):
            return None

    class Backend:
        async def connect_tcp(self, host, port, **kwargs):
            connected.append((host, port))
            return Stream()

    transport = PinnedAddressTransport(("93.184.216.34",))
    transport.transport._pool._network_backend = Backend()
    async with httpx.AsyncClient(transport=transport, trust_env=False) as client:
        response = await client.get("https://example.com:8443/path")
    assert response.text == "ok"
    assert connected == [("93.184.216.34", 8443)]
    assert tls == [("example.com", ssl.CERT_REQUIRED, True)]
    assert b"Host: example.com:8443\r\n" in b"".join(written)
