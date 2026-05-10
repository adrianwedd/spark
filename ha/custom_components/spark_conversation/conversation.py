from __future__ import annotations

import aiohttp
from homeassistant.components.conversation import ConversationEntity, ConversationInput, ConversationResult
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_URL, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([SparkConversationEntity(hass, entry)])


class SparkConversationEntity(ConversationEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_languages = ["en"]

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = entry.entry_id
        self._url = entry.data[CONF_URL]

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="SPARK",
            manufacturer="PiCar-X",
            model="Robot Assistant",
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_process(self, user_input: ConversationInput) -> ConversationResult:
        intent_response = intent.IntentResponse(language=user_input.language)

        try:
            session = async_get_clientsession(self.hass)
            async with session.post(
                f"{self._url}/api/v1/public/chat",
                json={"message": user_input.text},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                data = await resp.json()
            reply = data.get("reply") or "I'm here — just went quiet for a moment."
        except aiohttp.ClientConnectorError:
            reply = "I can't reach SPARK right now — it may be offline."
        except Exception:
            reply = "Something went wrong reaching SPARK."

        intent_response.async_set_speech(reply)
        return ConversationResult(
            response=intent_response,
            conversation_id=user_input.conversation_id,
        )
