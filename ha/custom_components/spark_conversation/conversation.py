from __future__ import annotations

import asyncio
import logging

import aiohttp
from homeassistant.components.conversation import ConversationEntity, ConversationInput, ConversationResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CONF_URL, DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([SparkConversationEntity(hass, entry)])


class SparkConversationEntity(ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None

    @property
    def supported_languages(self) -> list[str]:
        return ["en"]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._url = entry.data[CONF_URL]
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="SPARK",
            manufacturer="PiCar-X",
            model="Robot Assistant",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        intent_response = intent.IntentResponse(language=user_input.language)

        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                f"{self._url}/api/v1/public/chat",
                json={"message": user_input.text, "conversation_id": user_input.conversation_id},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
            reply = data.get("reply") or "I'm here — just went quiet for a moment."
        except asyncio.TimeoutError:
            _LOGGER.warning("SPARK request timed out")
            reply = "SPARK took too long to respond."
        except aiohttp.ClientConnectorError as err:
            _LOGGER.error("Cannot connect to SPARK at %s: %s", self._url, err)
            reply = "I can't reach SPARK right now — it may be offline."
        except aiohttp.ClientResponseError as err:
            _LOGGER.error("SPARK returned HTTP %s", err.status)
            reply = "SPARK returned an error response."
        except Exception:
            _LOGGER.exception("Unexpected error reaching SPARK")
            reply = "Something went wrong reaching SPARK."

        intent_response.async_set_speech(reply)
        return ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id,
        )
