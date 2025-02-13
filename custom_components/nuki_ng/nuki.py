from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.network import get_url
from homeassistant.components import webhook

import requests
import logging
import json
from datetime import timedelta
from urllib.parse import urlencode

from .constants import DOMAIN

_LOGGER = logging.getLogger(__name__)

BRIDGE_DISCOVERY_API = "https://api.nuki.io/discover/bridges"
BRIDGE_HOOK = "nuki_ng_bridge_hook"

class NukiInterface:

    def __init__(
        self, 
        hass, 
        *, 
        bridge: str = None,
        token: str = None,
        web_token: str = None
    ):
        self.hass = hass
        self.bridge = bridge
        self.token = token
        self.web_token = web_token

    async def async_json(self, cb):
        response = await self.hass.async_add_executor_job(lambda: cb(requests))
        if response.status_code >= 300:
            raise ConnectionError(f"Http response: {response.status_code}")
        if response.status_code > 200:
            return dict()
        json_resp = response.json()
        return json_resp

    async def discover_bridge(self) -> str:
        try:
            response = await self.async_json(
                lambda r: r.get(BRIDGE_DISCOVERY_API)
            )
            bridges = response.get("bridges", [])
            if len(bridges) > 0:
                return bridges[0]["ip"]
        except Exception as err:
            _LOGGER.exception(f"Failed to discover bridge:", err)
        return None

    def bridge_url(self, path: str, extra = None) -> str:
        extra_str = "&%s" % (urlencode(extra)) if extra else ""
        return f"http://{self.bridge}:8080{path}?token={self.token}{extra_str}"

    async def bridge_list(self):
        return await self.async_json(lambda r: r.get(self.bridge_url("/list")))

    async def bridge_info(self):
        return await self.async_json(lambda r: r.get(self.bridge_url("/info")))

    async def bridge_lock_action(self, dev_id: str, action: str):
        actions_map = {
            "unlock": 1,
            "lock": 2,
            "open": 3,
            "lock_n_go": 4,
            "lock_n_go_open": 5
        }
        return await self.async_json(
            lambda r: r.get(self.bridge_url(
                "/lockAction", 
                dict(action=actions_map[action], nukiId=dev_id)
            ))
        )

    async def bridge_check_callback(self, callback: str, add: bool = True):
        callbacks = await self.async_json(
            lambda r: r.get(self.bridge_url("/callback/list")
        ))
        _LOGGER.debug(f"bridge_check_callback: {callbacks}, {callback}")
        result = dict()
        for item in callbacks.get("callbacks", []):
            if item["url"] == callback:
                if add:
                    return None
                result = await self.async_json(
                    lambda r: r.get(self.bridge_url(
                        "/callback/remove",
                        {"id": item["id"]}
                    )
                ))
        if add:
            result = await self.async_json(
                lambda r: r.get(self.bridge_url(
                    "/callback/add", 
                    {"url": callback}
                )
            ))
        if not result.get("success", True):
            raise ConnectionError(result.get("message"))
    
    def web_url(self, path):
        return f"https://api.nuki.io{path}"
    
    async def web_async_json(self, cb):
        return await self.async_json(lambda r: cb(r, {
            "authorization": f"Bearer {self.web_token}"
        }))

    async def web_list_all_auths(self, dev_id: str):
        result = {}
        if not self.web_token:
            return result
        response = await self.web_async_json(
            lambda r, h: r.get(
                self.web_url(f"/smartlock/{dev_id}/auth"), 
                headers=h
            )
        )
        for item in response:
            result[item["id"]] = item
        return result

    async def web_update_auth(self, dev_id: str, auth_id: str, changes: dict):
        response = await self.web_async_json(
            lambda r, h: r.post(
                self.web_url(f"/smartlock/{dev_id}/auth/{auth_id}"), 
                headers=h,
                json=changes
            )
        )

class NukiCoordinator(DataUpdateCoordinator):

    def __init__(self, hass, entry, config: dict):
        self.entry = entry
        self.api = NukiInterface(
            hass, 
            bridge=config.get("address"), 
            token=config.get("token"), 
            web_token=config.get("web_token")
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_method=self._make_update_method(),
            update_interval=timedelta(seconds=30)
        )

        hook_id = "%s_%s" % (BRIDGE_HOOK, entry.entry_id)

        url = config.get("hass_url", get_url(hass))
        self.bridge_hook = "{}{}".format(url, webhook.async_generate_path(hook_id))
        webhook.async_unregister(hass, hook_id)
        webhook.async_register(
            hass,
            DOMAIN,
            "bridge",
            hook_id,
            handler=self._make_bridge_hook_handler(),
        )

    def _add_update(self, dev_id: str, update):
        data = self.data if self.data else {}
        previous = data.get(dev_id)
        if not previous:
            return None
        last_state = previous.get("lastKnownState", {})
        for key in last_state:
            if key in update:
                last_state[key] = update[key]
        previous["lastKnownState"] = last_state
        self.async_set_updated_data(data)

    async def _update(self):
        try:
            callback_updated = False
            try:
                await self.api.bridge_check_callback(self.bridge_hook)
                callback_updated = True
            except Exception:
                _LOGGER.exception(f"Failed to update callback {self.bridge_hook}")
            latest = await self.api.bridge_list()
            info = await self.api.bridge_info()
            previous = self.data if self.data else {}
            all_ids = set()
            mapped = dict()
            for item in latest:
                mapped[item["nukiId"]] = item
                all_ids.add(item["nukiId"])
            for item in previous:
                all_ids.add(item)
            for dev_id in all_ids:
                prev_web_auth = previous.get(dev_id, {}).get("web_auth", {})
                previous[dev_id] = mapped.get(dev_id, previous.get(dev_id))
                previous[dev_id]["callback_updated"] = callback_updated
                try:
                    previous[dev_id]["web_auth"] = await self.api.web_list_all_auths(dev_id)
                except ConnectionError:
                    _LOGGER.exception("Error while fetching auth:")
                    previous[dev_id]["web_auth"] = prev_web_auth
                previous[dev_id]["info"] = info
            _LOGGER.debug(f"_update: {json.dumps(previous)}")
            return previous
        except Exception as err:
            _LOGGER.exception(f"Failed to get latest data: {err}")
            raise UpdateFailed from err

    def _make_update_method(self):
        async def _update_data():
            return await self._update()
        return _update_data

    def _make_bridge_hook_handler(self):
        async def _hook_handler(hass, hook_id, request):
            body = await request.json()
            _LOGGER.debug(f"_hook_handler: {body}")
            self._add_update(body.get("nukiId"), body)

        return _hook_handler

    async def unload(self):
        try:
            result = await self.api.bridge_check_callback(self.bridge_hook, add=False)
            _LOGGER.debug(f"unload: {result} {self.bridge_hook}")
        except Exception:
            _LOGGER.exception(f"Failed to remove callback")

    async def action(self, dev_id: str, action: str):
        result = await self.api.bridge_lock_action(dev_id, action)
        if result.get("success"):
            await self.async_request_refresh()
        _LOGGER.debug(f"action result: {result}, {action}")

    def device_supports(self, dev_id: str, feature: str) -> bool:
        return feature in self.data.get(dev_id, {}).get("lastKnownState", {})

    async def update_web_auth(self, dev_id: str, auth: dict, changes: dict):
        if "id" not in auth:
            raise UpdateFailed("Invalid auth entry")
        await self.api.web_update_auth(dev_id, auth["id"], changes)
        data = self.data
        for key in changes:
            data.get(dev_id, {}).get("web_auth", {}).get(auth["id"], {})[key] = changes[key]
        self.async_set_updated_data(data)
