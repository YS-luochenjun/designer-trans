"""Bounded HTTP requests shared by provider adapters. No implicit retries."""

from .errors import ProviderError, UnknownExternalResult


def request_bytes(client, method, url, *, timeout, max_bytes, **kwargs):
    import httpx

    owned = client is None
    client = client or httpx.Client(follow_redirects=False)
    try:
        with client.stream(method, url, timeout=timeout, **kwargs) as response:
            if response.status_code == 408 or response.status_code >= 500:
                raise UnknownExternalResult(
                    f"Provider HTTP {response.status_code}; request may have been accepted, outcome unknown"
                )
            if response.status_code >= 400:
                raise ProviderError(f"Provider HTTP {response.status_code}")
            if response.status_code >= 300:
                raise ProviderError("Provider redirect refused")
            data = bytearray()
            for chunk in response.iter_bytes():
                if len(data) + len(chunk) > max_bytes:
                    raise ProviderError("Provider response exceeds size limit")
                data.extend(chunk)
            return bytes(data)
    except (httpx.TimeoutException, httpx.TransportError):
        raise UnknownExternalResult("Provider connection interrupted; outcome unknown") from None
    finally:
        if owned:
            client.close()
