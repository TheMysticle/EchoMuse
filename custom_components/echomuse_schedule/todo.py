from datetime import datetime, timezone
from homeassistant.components.todo import TodoListEntity, TodoItem, TodoItemStatus, TodoListEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import HomeAssistantError
from .api import DOMAIN
from .shared import is_shared


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([Reminders(entry.runtime_data)])


class Reminders(CoordinatorEntity, TodoListEntity):
    _attr_name = "Reminders"
    _attr_has_entity_name = True
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM | TodoListEntityFeature.DELETE_TODO_ITEM

    def __init__(self, coordinator):
        super().__init__(coordinator)
        serial = coordinator.entry.data["satellite"]
        self._attr_unique_id = serial + "_reminders"
        self._attr_device_info = {"identifiers": {(DOMAIN, serial)}}

    @property
    def todo_items(self):
        return [TodoItem(summary=i.get("message") or i["label"], uid=i["id"],
            status=TodoItemStatus.COMPLETED if i["state"] in ("completed", "missed", "canceled") else TodoItemStatus.NEEDS_ACTION,
            due=datetime.fromtimestamp(i["due"], timezone.utc) if i.get("due") else None,
            description=i["state"] + ("; recurring" if i.get("rule") else ""))
            for i in self.coordinator.data["items"] if i["kind"] == "reminder" and not is_shared(i)]

    async def async_update_todo_item(self, item):
        current = next((i for i in self.coordinator.data["items"] if i["id"] == item.uid and i["kind"] == "reminder"), None)
        if current is None:
            raise HomeAssistantError("Reminder not found")
        if item.status == TodoItemStatus.COMPLETED:
            action = "complete"
            await self.coordinator.command({"action": action, "id": item.uid})
        else:
            raise HomeAssistantError("Use echomuse_schedule.command to edit a scheduled reminder")

    async def async_delete_todo_items(self, uids):
        for uid in uids:
            await self.coordinator.command({"action": "cancel", "id": uid})
