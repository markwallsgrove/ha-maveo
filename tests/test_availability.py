"""Cloud and stick failures: availability, setup retries and action errors."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, State
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.maveo.api import Command, config as maveo_config
from custom_components.maveo.const import (
    ACCOUNT_POLL_INTERVAL,
    DEVICE_POLL_INTERVAL,
    DOMAIN,
    MISSED_POLLS_BEFORE_UNAVAILABLE,
)

from .fakes import DEVICE_ID, EMAIL, PASSWORD, CognitoPool, FakeMaveoApi, FakeStick


def _state(hass: HomeAssistant, platform: str, key: str) -> State:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DEVICE_ID}_{key}")
    assert entity_id is not None
    state = hass.states.get(entity_id)
    assert state is not None
    return state


async def _advance(hass: HomeAssistant, seconds: int) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done(wait_background_tasks=True)


def _cognito_unreachable(monkeypatch: pytest.MonkeyPatch, port: int) -> None:
    eu = maveo_config.Region.EU
    configs = dict(maveo_config._CONFIGS)
    configs[eu] = dataclasses.replace(
        configs[eu],
        cognito_idp_url=f"http://127.0.0.1:{port}/",
        cognito_identity_url=f"http://127.0.0.1:{port}/",
    )
    monkeypatch.setattr(maveo_config, "_CONFIGS", configs)


def _sign_out_with_new_password(cognito: CognitoPool) -> None:
    idp = cognito.idp()
    idp.admin_set_user_password(
        UserPoolId=cognito.user_pool_id,
        Username=EMAIL,
        Password="Changed-password-2",
        Permanent=True,
    )
    idp.admin_user_global_sign_out(UserPoolId=cognito.user_pool_id, Username=EMAIL)


def _break(stick: FakeStick, failure: str) -> None:
    if failure == "unreachable":
        stick.reachable = False
    elif failure == "connack_refused":
        stick.connack_code = 5
    else:
        stick.replies = {}


@pytest.mark.parametrize("failure", ["unreachable", "connack_refused", "silent"])
async def test_stick_keeps_last_state_until_three_polls_fail(
    hass: HomeAssistant, entry: ConfigEntry, stick: FakeStick, failure: str
) -> None:
    _break(stick, failure)
    for poll in range(1, MISSED_POLLS_BEFORE_UNAVAILABLE):
        await _advance(hass, poll * DEVICE_POLL_INTERVAL + 1)
        assert _state(hass, "cover", "cover").state == "closed"
        assert _state(hass, "binary_sensor", "connectivity").state == "on"

    await _advance(hass, MISSED_POLLS_BEFORE_UNAVAILABLE * DEVICE_POLL_INTERVAL + 1)
    assert _state(hass, "cover", "cover").state == "unavailable"
    assert _state(hass, "light", "light").state == "unavailable"
    connectivity = "off" if failure == "silent" else "unavailable"
    assert _state(hass, "binary_sensor", "connectivity").state == connectivity


@pytest.mark.parametrize("failure", ["unreachable", "connack_refused"])
async def test_unreachable_stick_recovers(
    hass: HomeAssistant, entry: ConfigEntry, stick: FakeStick, failure: str
) -> None:
    _break(stick, failure)
    for poll in range(1, MISSED_POLLS_BEFORE_UNAVAILABLE + 1):
        await _advance(hass, poll * DEVICE_POLL_INTERVAL + 1)
    assert _state(hass, "cover", "cover").state == "unavailable"

    stick.reachable = True
    stick.connack_code = 0
    await _advance(hass, (MISSED_POLLS_BEFORE_UNAVAILABLE + 1) * DEVICE_POLL_INTERVAL + 1)
    assert _state(hass, "cover", "cover").state == "closed"
    assert _state(hass, "binary_sensor", "connectivity").state == "on"


async def test_silent_stick_is_only_asked_for_the_door(
    hass: HomeAssistant, entry: ConfigEntry, stick: FakeStick
) -> None:
    stick.replies = {}
    stick.commands.clear()
    await _advance(hass, DEVICE_POLL_INTERVAL + 1)
    assert stick.commands == [Command.STATUS]


async def test_command_to_unreachable_stick_raises(hass: HomeAssistant, entry: ConfigEntry, stick: FakeStick) -> None:
    stick.reachable = False
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "light",
            "turn_off",
            {"entity_id": _state(hass, "light", "light").entity_id},
            blocking=True,
        )
    assert err.value.translation_key == "command_failed"


async def test_command_with_rejected_password_starts_reauth(
    hass: HomeAssistant, entry: ConfigEntry, cognito: CognitoPool
) -> None:
    _sign_out_with_new_password(cognito)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            "cover",
            "open_cover",
            {"entity_id": _state(hass, "cover", "cover").entity_id},
            blocking=True,
        )
    assert err.value.translation_key == "auth_failed"
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_unreachable_cloud_retries_setup(
    hass: HomeAssistant,
    entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    _cognito_unreachable(monkeypatch, unused_tcp_port)
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_rest_api_down_retries_setup(hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi) -> None:
    cloud.fail_with = 503
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_account_poll_failure_keeps_devices(hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi) -> None:
    cloud.fail_with = 503
    await _advance(hass, ACCOUNT_POLL_INTERVAL + 1)
    assert DEVICE_ID in entry.runtime_data.devices
    assert _state(hass, "cover", "cover").state == "closed"


async def test_account_without_devices_loads(hass: HomeAssistant, cloud: FakeMaveoApi) -> None:
    cloud.devices.clear()
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD, "region": "EU"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert not er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


async def test_entry_without_refresh_token_signs_in_with_password(hass: HomeAssistant, cloud: FakeMaveoApi) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD, "region": "EU"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.data["refresh_token"]


async def test_service_rejects_unknown_device(hass: HomeAssistant, entry: ConfigEntry, cloud: FakeMaveoApi) -> None:
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "create_guest",
            {"device_id": "not-a-device"},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "device_not_found"


async def test_service_rejects_device_not_on_account(hass: HomeAssistant, entry: ConfigEntry) -> None:
    device = dr.async_get(hass).async_get_or_create(config_entry_id=entry.entry_id, identifiers={(DOMAIN, "9999")})
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "create_guest", {"device_id": device.id}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "device_not_found"


async def test_service_rejects_device_of_unloaded_entry(
    hass: HomeAssistant, entry: ConfigEntry, garage_device_id: str
) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN, "create_guest", {"device_id": garage_device_id}, blocking=True, return_response=True
        )
    assert err.value.translation_key == "device_not_found"


async def test_create_guest_api_error_raises(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cloud: FakeMaveoApi,
    garage_device_id: str,
) -> None:
    cloud.fail_with = 500
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "create_guest",
            {"device_id": garage_device_id},
            blocking=True,
            return_response=True,
        )
    assert err.value.translation_key == "guest_create_failed"


async def test_service_with_unreachable_cloud_raises(
    hass: HomeAssistant,
    entry: ConfigEntry,
    garage_device_id: str,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    # Credentials from moto expire at once, so the service renews them first.
    _cognito_unreachable(monkeypatch, unused_tcp_port)
    entry.runtime_data.session.config = maveo_config.get_config(maveo_config.Region.EU)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "remove_guest",
            {"device_id": garage_device_id, "user_id": "u-1"},
            blocking=True,
        )
    assert err.value.translation_key == "cloud_unreachable"


async def test_unreachable_cloud_during_poll_makes_entities_unavailable(
    hass: HomeAssistant,
    entry: ConfigEntry,
    monkeypatch: pytest.MonkeyPatch,
    unused_tcp_port: int,
) -> None:
    # Credentials from moto expire at once, so each poll renews them first.
    _cognito_unreachable(monkeypatch, unused_tcp_port)
    entry.runtime_data.session.config = maveo_config.get_config(maveo_config.Region.EU)
    await _advance(hass, DEVICE_POLL_INTERVAL + 1)
    assert _state(hass, "cover", "cover").state == "unavailable"


async def test_valid_credentials_are_not_renewed(hass: HomeAssistant, entry: ConfigEntry, stick: FakeStick) -> None:
    session = entry.runtime_data.session
    session.auth = dataclasses.replace(session.auth, expiration=datetime(2100, 1, 1, tzinfo=UTC))
    auth = session.auth
    await hass.services.async_call(
        "light", "turn_off", {"entity_id": _state(hass, "light", "light").entity_id}, blocking=True
    )
    assert session.auth is auth
    assert Command.LIGHT_OFF in stick.commands


async def test_service_with_rejected_password_starts_reauth(
    hass: HomeAssistant,
    entry: ConfigEntry,
    cognito: CognitoPool,
    garage_device_id: str,
) -> None:
    _sign_out_with_new_password(cognito)
    with pytest.raises(HomeAssistantError) as err:
        await hass.services.async_call(
            DOMAIN,
            "remove_guest",
            {"device_id": garage_device_id, "user_id": "u-1"},
            blocking=True,
        )
    assert err.value.translation_key == "auth_failed"
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == [SOURCE_REAUTH]


async def test_malformed_stick_replies_are_tolerated(
    hass: HomeAssistant, cloud: FakeMaveoApi, stick: FakeStick
) -> None:
    stick.ht_sensor = {"StoA_sensor": 0, "temperature_val": 2150}
    stick.replies["AtoS_weather"] = {"StoA_weather": 0, "temperature": "warm"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD, "region": "EU"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert _state(hass, "sensor", "indoor_temperature").state == "21.5"
    assert _state(hass, "sensor", "indoor_humidity").state == "unknown"
    assert _state(hass, "sensor", "weather_temperature").state == "unknown"
    assert _state(hass, "cover", "cover").state == "closed"
