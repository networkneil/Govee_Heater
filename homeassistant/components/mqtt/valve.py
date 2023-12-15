"""Support for MQTT valve devices."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.components import valve
from homeassistant.components.valve import (
    DEVICE_CLASSES_SCHEMA,
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_NAME,
    CONF_OPTIMISTIC,
    CONF_VALUE_TEMPLATE,
    STATE_CLOSED,
    STATE_CLOSING,
    STATE_OPEN,
    STATE_OPENING,
)
from homeassistant.core import HomeAssistant, callback
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType
from homeassistant.util.percentage import (
    percentage_to_ranged_value,
    ranged_value_to_percentage,
)

from . import subscription
from .config import MQTT_BASE_SCHEMA
from .const import (
    CONF_COMMAND_TEMPLATE,
    CONF_COMMAND_TOPIC,
    CONF_ENCODING,
    CONF_QOS,
    CONF_RETAIN,
    CONF_STATE_TOPIC,
    DEFAULT_OPTIMISTIC,
)
from .debug_info import log_messages
from .mixins import (
    MQTT_ENTITY_COMMON_SCHEMA,
    MqttEntity,
    async_setup_entity_entry_helper,
    write_state_on_attr_change,
)
from .models import MqttCommandTemplate, MqttValueTemplate, ReceiveMessage
from .util import valid_publish_topic, valid_subscribe_topic

_LOGGER = logging.getLogger(__name__)

CONF_POSITION = "position"

CONF_PAYLOAD_CLOSE = "payload_close"
CONF_PAYLOAD_OPEN = "payload_open"
CONF_PAYLOAD_STOP = "payload_stop"
CONF_POSITION_CLOSED = "position_closed"
CONF_POSITION_OPEN = "position_open"
CONF_STATE_CLOSED = "state_closed"
CONF_STATE_CLOSING = "state_closing"
CONF_STATE_OPEN = "state_open"
CONF_STATE_OPENING = "state_opening"

DEFAULT_NAME = "MQTT Valve"
DEFAULT_PAYLOAD_CLOSE = "CLOSE"
DEFAULT_PAYLOAD_OPEN = "OPEN"
DEFAULT_POSITION_CLOSED = 0
DEFAULT_POSITION_OPEN = 100
DEFAULT_RETAIN = False

MQTT_VALVE_ATTRIBUTES_BLOCKED = frozenset(
    {
        valve.ATTR_CURRENT_POSITION,
    }
)


_PLATFORM_SCHEMA_BASE = MQTT_BASE_SCHEMA.extend(
    {
        vol.Optional(CONF_COMMAND_TOPIC): valid_publish_topic,
        vol.Optional(CONF_COMMAND_TEMPLATE): cv.template,
        vol.Optional(CONF_DEVICE_CLASS): vol.Any(DEVICE_CLASSES_SCHEMA, None),
        vol.Optional(CONF_POSITION, default=False): cv.boolean,
        vol.Optional(CONF_NAME): vol.Any(cv.string, None),
        vol.Optional(CONF_OPTIMISTIC, default=DEFAULT_OPTIMISTIC): cv.boolean,
        vol.Optional(CONF_PAYLOAD_CLOSE, default=DEFAULT_PAYLOAD_CLOSE): cv.string,
        vol.Optional(CONF_PAYLOAD_OPEN, default=DEFAULT_PAYLOAD_OPEN): cv.string,
        vol.Optional(CONF_PAYLOAD_STOP): vol.Any(cv.string, None),
        vol.Optional(CONF_POSITION_CLOSED, default=DEFAULT_POSITION_CLOSED): int,
        vol.Optional(CONF_POSITION_OPEN, default=DEFAULT_POSITION_OPEN): int,
        vol.Optional(CONF_RETAIN, default=DEFAULT_RETAIN): cv.boolean,
        vol.Optional(CONF_STATE_CLOSED, default=STATE_CLOSED): cv.string,
        vol.Optional(CONF_STATE_CLOSING, default=STATE_CLOSING): cv.string,
        vol.Optional(CONF_STATE_OPEN, default=STATE_OPEN): cv.string,
        vol.Optional(CONF_STATE_OPENING, default=STATE_OPENING): cv.string,
        vol.Optional(CONF_STATE_TOPIC): valid_subscribe_topic,
        vol.Optional(CONF_VALUE_TEMPLATE): cv.template,
    }
).extend(MQTT_ENTITY_COMMON_SCHEMA.schema)

PLATFORM_SCHEMA_MODERN = vol.All(
    _PLATFORM_SCHEMA_BASE,
)

DISCOVERY_SCHEMA = vol.All(
    _PLATFORM_SCHEMA_BASE.extend({}, extra=vol.REMOVE_EXTRA),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up MQTT valve through YAML and through MQTT discovery."""
    await async_setup_entity_entry_helper(
        hass,
        config_entry,
        MqttValve,
        valve.DOMAIN,
        async_add_entities,
        DISCOVERY_SCHEMA,
        PLATFORM_SCHEMA_MODERN,
    )


class MqttValve(MqttEntity, ValveEntity):
    """Representation of a valve that can be controlled using MQTT."""

    _attr_is_closed: bool | None = None
    _attributes_extra_blocked: frozenset[str] = MQTT_VALVE_ATTRIBUTES_BLOCKED
    _default_name = DEFAULT_NAME
    _entity_id_format: str = valve.ENTITY_ID_FORMAT
    _optimistic: bool
    _range: tuple[int, int]
    _tilt_optimistic: bool

    @staticmethod
    def config_schema() -> vol.Schema:
        """Return the config schema."""
        return DISCOVERY_SCHEMA

    def _setup_from_config(self, config: ConfigType) -> None:
        """Set up valve from config."""
        self._attr_reports_position = config[CONF_POSITION]
        self._range = (
            self._config[CONF_POSITION_CLOSED] + 1,
            self._config[CONF_POSITION_OPEN],
        )
        no_state_topic = config.get(CONF_STATE_TOPIC) is None
        self._optimistic = config[CONF_OPTIMISTIC] or no_state_topic
        self._attr_assumed_state = self._optimistic

        template_config_attributes = {
            "position_open": config[CONF_POSITION_OPEN],
            "position_closed": config[CONF_POSITION_CLOSED],
        }

        self._value_template = MqttValueTemplate(
            config.get(CONF_VALUE_TEMPLATE), entity=self
        ).async_render_with_possible_json_value

        self._command_template = MqttCommandTemplate(
            config.get(CONF_COMMAND_TEMPLATE), entity=self
        ).async_render

        self._value_template = MqttValueTemplate(
            config.get(CONF_VALUE_TEMPLATE),
            entity=self,
            config_attributes=template_config_attributes,
        ).async_render_with_possible_json_value

        self._attr_device_class = config.get(CONF_DEVICE_CLASS)

        supported_features = ValveEntityFeature(0)
        if has_command_topic := (config.get(CONF_COMMAND_TOPIC) is not None):
            supported_features |= ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

        if config[CONF_POSITION] and has_command_topic is not None:
            supported_features |= ValveEntityFeature.SET_POSITION
        if config.get(CONF_PAYLOAD_STOP) is not None:
            supported_features |= ValveEntityFeature.STOP

        self._attr_supported_features = supported_features

    @callback
    def _update_state(self, state: str) -> None:
        """Update the valve state based on static payload."""
        self._attr_is_closed = state == STATE_CLOSED
        self._attr_is_opening = state == STATE_OPENING
        self._attr_is_closing = state == STATE_CLOSING

    def _prepare_subscribe_topics(self) -> None:
        """(Re)Subscribe to topics."""
        topics = {}

        @callback
        @log_messages(self.hass, self.entity_id)
        @write_state_on_attr_change(
            self,
            {
                "_attr_current_valve_position",
                "_attr_is_closed",
                "_attr_is_closing",
                "_attr_is_opening",
            },
        )
        def state_message_received(msg: ReceiveMessage) -> None:
            """Handle new MQT/T state messages."""
            payload = self._value_template(msg.payload)

            if not payload:
                _LOGGER.debug("Ignoring empty state message from '%s'", msg.topic)
                return

            if self._config[CONF_POSITION]:
                try:
                    percentage_payload = ranged_value_to_percentage(
                        self._range, float(payload)
                    )
                except ValueError:
                    _LOGGER.warning("Payload '%s' is not numeric", payload)
                    return

                self._attr_current_valve_position = percentage_payload
                return

            state: str
            if payload == self._config[CONF_STATE_OPENING]:
                state = STATE_OPENING
            elif payload == self._config[CONF_STATE_CLOSING]:
                state = STATE_CLOSING
            elif payload == self._config[CONF_STATE_OPEN]:
                state = STATE_OPEN
            elif payload == self._config[CONF_STATE_CLOSED]:
                state = STATE_CLOSED
            else:
                _LOGGER.warning(
                    (
                        "Payload is not supported (e.g. open, closed, opening, closing)"
                        ": %s"
                    ),
                    payload,
                )
                return
            self._update_state(state)

        if self._config.get(CONF_STATE_TOPIC):
            topics["state_topic"] = {
                "topic": self._config.get(CONF_STATE_TOPIC),
                "msg_callback": state_message_received,
                "qos": self._config[CONF_QOS],
                "encoding": self._config[CONF_ENCODING] or None,
            }

        self._sub_state = subscription.async_prepare_subscribe_topics(
            self.hass, self._sub_state, topics
        )

    async def _subscribe_topics(self) -> None:
        """(Re)Subscribe to topics."""
        await subscription.async_subscribe_topics(self.hass, self._sub_state)

    async def async_open_valve(self) -> None:
        """Move the valve up.

        This method is a coroutine.
        """
        payload = self._command_template(self._config[CONF_PAYLOAD_OPEN])
        await self.async_publish(
            self._config[CONF_COMMAND_TOPIC],
            payload,
            self._config[CONF_QOS],
            self._config[CONF_RETAIN],
            self._config[CONF_ENCODING],
        )
        if self._optimistic:
            # Optimistically assume that valve has changed state.
            self._update_state(STATE_OPEN)
            self.async_write_ha_state()

    async def async_close_valve(self) -> None:
        """Move the valve down.

        This method is a coroutine.
        """
        payload = self._command_template(self._config[CONF_PAYLOAD_CLOSE])
        await self.async_publish(
            self._config[CONF_COMMAND_TOPIC],
            payload,
            self._config[CONF_QOS],
            self._config[CONF_RETAIN],
            self._config[CONF_ENCODING],
        )
        if self._optimistic:
            # Optimistically assume that valve has changed state.
            self._update_state(STATE_CLOSED)
            self.async_write_ha_state()

    async def async_stop_valve(self) -> None:
        """Stop valve positioning.

        This method is a coroutine.
        """
        payload = self._command_template(self._config[CONF_PAYLOAD_STOP])
        await self.async_publish(
            self._config[CONF_COMMAND_TOPIC],
            payload,
            self._config[CONF_QOS],
            self._config[CONF_RETAIN],
            self._config[CONF_ENCODING],
        )

    async def async_set_valve_position(self, position: int) -> None:
        """Move the valve to a specific position."""
        percentage_position = position
        scaled_position = round(
            percentage_to_ranged_value(self._range, percentage_position)
        )
        variables = {
            "position": percentage_position,
            "position_open": self._config[CONF_POSITION_OPEN],
            "position_closed": self._config[CONF_POSITION_CLOSED],
        }
        rendered_position = self._command_template(scaled_position, variables=variables)

        await self.async_publish(
            self._config[CONF_COMMAND_TOPIC],
            rendered_position,
            self._config[CONF_QOS],
            self._config[CONF_RETAIN],
            self._config[CONF_ENCODING],
        )
        if self._optimistic:
            self._update_state(
                STATE_CLOSED
                if percentage_position == self._config[CONF_POSITION_CLOSED]
                else STATE_OPEN
            )
            self._attr_current_valve_position = percentage_position
            self.async_write_ha_state()
