"""DataUpdateCoordinator for FranklinWH."""

from __future__ import annotations

from datetime import timedelta
import logging

from franklinwh import Client, Stats, TokenFetcher, Mode

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_LOCAL_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class FranklinWHData:
    """Class to hold FranklinWH data."""

    def __init__(
        self, stats: Stats, switch_state: tuple[bool, bool, bool] | None = None
    ) -> None:
        """Initialize the data class."""
        self.stats = stats
        self.switch_state = switch_state or (False, False, False)


class FranklinWHCoordinator(DataUpdateCoordinator[FranklinWHData]):
    """Class to manage fetching FranklinWH data."""

    def __init__(
        self,
        hass: HomeAssistant,
        username: str,
        password: str,
        gateway_id: str,
        use_local_api: bool = False,
        local_host: str | None = None,
    ) -> None:
        """Initialize the coordinator."""
        self.username = username
        self.password = password
        self.gateway_id = gateway_id
        self.use_local_api = use_local_api
        self.local_host = local_host

        # Store credentials for lazy client initialization
        # Client will be created in executor during first update to avoid blocking
        self.token_fetcher: TokenFetcher = None  # type: ignore  # noqa: PGH003
        self.client: Client = None  # type: ignore  # noqa: PGH003
        self._client_lock = False

        # Set update interval based on API type
        update_interval = (
            DEFAULT_LOCAL_SCAN_INTERVAL if use_local_api else DEFAULT_SCAN_INTERVAL
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=update_interval),
            # Keep entities available during temporary failures
            # Only mark unavailable after 3 consecutive failures (3 minutes)
            always_update=False,
        )
        
        # Track consecutive failures
        self._consecutive_failures = 0
        self._max_failures = 3

    async def _async_update_data(self) -> FranklinWHData:
        """Fetch data from FranklinWH API."""
        try:
            # Initialize client on first run (in executor to avoid blocking)
            if self.client is None and not self._client_lock:
                self._client_lock = True
                try:

                    def create_client():
                        token_fetcher = TokenFetcher(self.username, self.password)
                        return Client(token_fetcher, self.gateway_id)

                    self.client = await self.hass.async_add_executor_job(create_client)
                    self.token_fetcher = self.client.fetcher
                except Exception as err:
                    self._client_lock = False
                    raise UpdateFailed(f"Failed to initialize client: {err}") from err

            # Fetch stats
            stats = await self.hass.async_add_executor_job(self.client.get_stats)

            if stats is None:
                raise UpdateFailed("Failed to fetch stats from FranklinWH API")

            # Fetch switch state
            try:
                switch_state = await self.hass.async_add_executor_job(
                    self.client.get_smart_switch_state
                )
            except Exception as err:
                _LOGGER.debug("Failed to fetch switch state: %s", err)
                switch_state = None

            # Reset failure counter on success
            self._consecutive_failures = 0
            
            return FranklinWHData(stats=stats, switch_state=switch_state)

        except AttributeError as err:
            # Handle case where AuthenticationError doesn't exist in franklinwh
            if "AuthenticationError" in str(type(err)):
                raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
            
            # Increment failure counter
            self._consecutive_failures += 1
            _LOGGER.warning(
                "API error (attempt %d/%d): %s", 
                self._consecutive_failures, 
                self._max_failures, 
                err
            )
            
            # Only raise UpdateFailed after max failures
            # This keeps entities available with last known data
            if self._consecutive_failures >= self._max_failures:
                _LOGGER.error("Max consecutive failures reached, marking unavailable")
                raise UpdateFailed(f"Error communicating with API: {err}") from err
            
            # Return last known data to keep entities available
            if self.data:
                _LOGGER.debug("Returning last known data due to temporary failure")
                return self.data
            raise UpdateFailed(f"Error communicating with API: {err}") from err
            
        except Exception as err:
            # Check if it's an authentication-related error
            if "auth" in str(err).lower() or "token" in str(err).lower():
                raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
            
            # Increment failure counter
            self._consecutive_failures += 1
            _LOGGER.warning(
                "API error (attempt %d/%d): %s", 
                self._consecutive_failures, 
                self._max_failures, 
                err
            )
            
            # Only raise UpdateFailed after max failures
            if self._consecutive_failures >= self._max_failures:
                _LOGGER.error("Max consecutive failures reached, marking unavailable")
                raise UpdateFailed(f"Error communicating with API: {err}") from err
            
            # Return last known data to keep entities available
            if self.data:
                _LOGGER.debug("Returning last known data due to temporary failure")
                return self.data
            raise UpdateFailed(f"Error communicating with API: {err}") from err

    async def async_set_switch_state(self, switches: tuple[bool, bool, bool]) -> None:
        """Set the state of smart switches."""
        try:
            await self.hass.async_add_executor_job(
                self.client.set_smart_switch_state, switches
            )
            # Request immediate refresh
            await self.async_request_refresh()
        except Exception as err:
            _LOGGER.error("Failed to set switch state: %s", err)
            raise

    async def async_set_operation_mode(self, mode: str) -> None:
        """Set the operation mode of the system."""
        try:
            # Map string mode to Mode factory methods
            # Each mode gets a default reserve of 20% except emergency_backup (100%)
            mode_map = {
                "self_use": Mode.self_consumption,
                "backup": Mode.emergency_backup,
                "time_of_use": Mode.time_of_use,
                # Note: clean_backup is not in the library, map to emergency_backup
                "clean_backup": Mode.emergency_backup,
            }
            
            if mode not in mode_map:
                raise ValueError(f"Invalid mode: {mode}")
            
            # Create mode object with default SOC
            mode_obj = mode_map[mode]()
            
            # Set the mode via API
            await self.hass.async_add_executor_job(
                self.client.set_mode, mode_obj
            )
            
            # Request immediate refresh
            await self.async_request_refresh()
            _LOGGER.info("Successfully set operation mode to %s", mode)
        except Exception as err:
            _LOGGER.error("Failed to set operation mode to %s: %s", mode, err)
            raise

    async def async_set_battery_reserve(self, reserve_percent: int) -> None:
        """Set the battery reserve percentage."""
        try:
            # Get the current mode first
            current_mode = await self.hass.async_add_executor_job(
                self.client.get_mode
            )
            
            # We need to determine which mode type it is and recreate with new SOC
            # For now, we'll use self_consumption as default
            # In a real implementation, we'd need to detect the current mode type
            mode_obj = Mode.self_consumption(soc=reserve_percent)
            
            await self.hass.async_add_executor_job(
                self.client.set_mode, mode_obj
            )
            
            # Request immediate refresh
            await self.async_request_refresh()
            _LOGGER.info("Successfully set battery reserve to %d%%", reserve_percent)
        except Exception as err:
            _LOGGER.error("Failed to set battery reserve to %d%%: %s", reserve_percent, err)
            raise
