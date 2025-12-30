"""Utilities for FranklinWH integration."""

import franklinwh
from franklinwh.client import Client, TokenFetcher
import httpx

from homeassistant.core import HomeAssistant


async def get_client(
    hass: HomeAssistant,
    username: str,
    password: str,
    gateway_id: str,
):
    """Create and return a franklinwh TokenFetcher and Client."""

    async def _get_client() -> httpx.AsyncClient:
        return await hass.async_add_executor_job(lambda: httpx.AsyncClient(http2=True))

    franklinwh.HttpClientFactory.set_client_factory(_get_client)
    token_fetcher = TokenFetcher(username, password)
    client = Client(token_fetcher, gateway_id)
    return (token_fetcher, client)
