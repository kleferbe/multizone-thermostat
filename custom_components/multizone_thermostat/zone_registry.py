"""Membership of satellite roles under a master role, keyed by full entity_id."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from . import DOMAIN

if TYPE_CHECKING:
    from .master_role import MasterRole
    from .satellite_role import SatelliteRole

DATA_REGISTRY = "zone_registry"


def async_get_registry(hass) -> ZoneRegistry:
    """Return the shared zone registry for this Home Assistant instance."""
    data = hass.data.setdefault(DOMAIN, {})
    if DATA_REGISTRY not in data:
        data[DATA_REGISTRY] = ZoneRegistry()
    return data[DATA_REGISTRY]


class ZoneRegistry:
    """Satellites may register before the master role exists."""

    def __init__(self) -> None:
        self.members: dict[str, dict[str, SatelliteRole]] = defaultdict(dict)
        self.masters: dict[str, MasterRole] = {}

    def master(self, master_id: str) -> MasterRole | None:
        """Return the live master role, if it has been added to hass."""
        return self.masters.get(master_id)

    def satellites(self, master_id: str) -> dict[str, SatelliteRole]:
        """Registered satellite roles for a master (may be empty)."""
        return self.members.get(master_id, {})

    def member_ids(self, master_id: str) -> list[str]:
        """Stable satellite entity_ids for a master."""
        return sorted(self.satellites(master_id))

    def satellite(self, master_id: str, sat_id: str) -> SatelliteRole | None:
        """Return a registered satellite role."""
        return self.satellites(master_id).get(sat_id)

    def register_satellite(self, sat: SatelliteRole, master_id: str) -> None:
        """Add a satellite and enroll it with the master when present."""
        self.members[master_id][sat.entity.entity_id] = sat
        master = self.master(master_id)
        if master is not None:
            master.enroll_satellite(sat)

    def unregister_satellite(self, sat: SatelliteRole, master_id: str) -> None:
        """Remove a satellite and unenroll it from the master when present."""
        group = self.members.get(master_id)
        if group:
            group.pop(sat.entity.entity_id, None)
        master = self.master(master_id)
        if master is not None:
            master.unenroll_satellite(sat)

    def register_master(self, master: MasterRole) -> None:
        """Add a master and enroll satellites already waiting on its entity_id."""
        self.masters[master.entity.entity_id] = master
        for sat in list(self.satellites(master.entity.entity_id).values()):
            master.enroll_satellite(sat)

    def rekey_entity(self, old_id: str, new_id: str) -> None:
        """Follow an entity_id change for a master or satellite."""
        if old_id == new_id:
            return
        if master := self.masters.get(old_id):
            self.masters.pop(old_id)
            self.masters[new_id] = master
            if old_id in self.members:
                dest = self.members[new_id]
                dest.update(self.members.pop(old_id))
                for sat in dest.values():
                    sat.master_id = new_id
            for sat in list(self.satellites(new_id).values()):
                master.enroll_satellite(sat)
            return
        for master_id, group in self.members.items():
            if old_id not in group:
                continue
            sat = group.pop(old_id)
            group[new_id] = sat
            if master := self.master(master_id):
                master.enroll_satellite(sat)
            return

    def unregister_master(self, master: MasterRole) -> None:
        """Drop the master role; satellite membership stays."""
        for key, current in list(self.masters.items()):
            if current is master:
                self.masters.pop(key, None)
