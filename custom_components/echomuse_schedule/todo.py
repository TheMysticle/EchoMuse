import copy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from homeassistant.components.todo import TodoListEntity, TodoItem, TodoItemStatus, TodoListEntityFeature
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.exceptions import HomeAssistantError
from .api import DOMAIN
from .shared import is_shared


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([Reminders(entry.runtime_data)])


FINISHED = ("completed", "missed", "canceled")
# The card sends its time back to the minute; a reminder set "in 5 minutes" has seconds.
SAME_TIME_S = 60


def changed_reminder(current, item):
    """The replacement for `current` if the card changed its words or its time, else None.

    The device resets any reminder whose deadline changes to scheduled, so a
    finished one would ring again and a shared copy is overwritten by its
    original on the next change; both are refused rather than half done.
    """
    summary = (item.summary or "").strip()
    words = current.get("message") or current["label"]
    new = copy.deepcopy(current)
    changed = False
    if summary and summary != words:
        new["message"] = summary
        if current.get("label") in ("", None, "reminder", current.get("message")):
            new["label"] = summary[:160]
        new.pop("speech", None)
        new.pop("speech_version", None)
        changed = True
    due = item.due
    if isinstance(due, datetime):
        zone = ZoneInfo(current.get("timezone") or "UTC")
        if due.tzinfo is None:
            due = due.replace(tzinfo=zone)
        stamp = int(due.timestamp())
        if current.get("due") and abs(stamp - current["due"]) >= SAME_TIME_S or not current.get("due") and not current.get("rule"):
            if current.get("rule"):
                raise HomeAssistantError("A repeating reminder keeps its days; delete it and add a new one to change them")
            new["due"] = stamp
            changed = True
    elif due is None and current.get("due") and not current.get("rule"):
        raise HomeAssistantError("A reminder needs a time")
    if not changed:
        return None
    if current["state"] in FINISHED:
        raise HomeAssistantError("That reminder has finished; add a new one")
    if is_shared(current):
        raise HomeAssistantError("That is a copy shared from another Echo; change the original")
    return new


class Reminders(CoordinatorEntity, TodoListEntity):
    _attr_name = "Reminders"
    _attr_has_entity_name = True
    _attr_supported_features = (TodoListEntityFeature.UPDATE_TODO_ITEM | TodoListEntityFeature.DELETE_TODO_ITEM
                                | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM)

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
            description=i["state"] + ("; recurring" if i.get("rule") else "") + ("; shared" if is_shared(i) else ""))
            for i in self.coordinator.data["items"] if i["kind"] == "reminder"]

    async def async_update_todo_item(self, item):
        current = next((i for i in self.coordinator.data["items"] if i["id"] == item.uid and i["kind"] == "reminder"), None)
        if current is None:
            raise HomeAssistantError("Reminder not found")
        finished = current["state"] in FINISHED
        if item.status == TodoItemStatus.COMPLETED:
            if not finished:
                await self.coordinator.command({"action": "complete", "id": item.uid})
            return
        edit = changed_reminder(current, item)
        if edit is None:
            if finished:
                raise HomeAssistantError("That reminder has finished; add a new one")
            return
        await self.coordinator.command({"action": "edit", "id": item.uid, "item": edit})

    async def async_delete_todo_items(self, uids):
        for uid in uids:
            await self.coordinator.command({"action": "cancel", "id": uid})
