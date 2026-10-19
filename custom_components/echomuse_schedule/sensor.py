from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers import entity_registry as er
from .api import DOMAIN
from .shared import is_shared


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    known = {}

    def sync():
        if not coordinator.last_update_success:
            return
        items = {i["id"]: i for i in coordinator.data["items"] if i["kind"] in ("timer", "alarm") and not is_shared(i)}
        for uid in list(known):
            if uid not in items:
                entity = known.pop(uid)
                registry = er.async_get(hass)
                if entity_id := registry.async_get_entity_id("sensor", DOMAIN, entity.unique_id):
                    registry.async_remove(entity_id)
        added = []
        for uid in items:
            if uid not in known:
                known[uid] = ItemSensor(coordinator, uid)
                added.append(known[uid])
        if added:
            async_add_entities(added)
        # Remove stale registry entries even after HA restarted during cleanup.
        active = {entity.unique_id for entity in known.values()}
        registry = er.async_get(hass)
        for entity in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
            if entity.domain == "sensor" and entity.unique_id not in active:
                registry.async_remove(entity.entity_id)

    entry.async_on_unload(coordinator.async_add_listener(sync))
    sync()


class ItemSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, uid):
        super().__init__(coordinator)
        self.uid = uid
        self._attr_unique_id = coordinator.entry.data["satellite"] + "_" + uid
        self._attr_device_info = {"identifiers": {(DOMAIN, coordinator.entry.data["satellite"])}}

    @property
    def item(self):
        return next((i for i in self.coordinator.data["items"] if i["id"] == self.uid), None)

    @property
    def name(self):
        i = self.item
        return (i["kind"].title() + " " + i["label"]) if i else self.uid

    @property
    def native_value(self):
        return self.item["state"] if self.item else None

    @property
    def available(self):
        return super().available and self.item is not None

    @property
    def extra_state_attributes(self):
        return dict(self.item or {})
