"""Circuit membership: climate entity_id → Climate object, keyed by current entity_id."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import DOMAIN

if TYPE_CHECKING:
    from .climate import MultiZoneThermostat
    from .select import CircuitSelect

DATA_REGISTRY = "zone_registry"


def async_get_registry(hass) -> ZoneRegistry:
    """Return the shared zone registry for this Home Assistant instance."""
    data = hass.data.setdefault(DOMAIN, {})
    if DATA_REGISTRY not in data:
        data[DATA_REGISTRY] = ZoneRegistry()
    return data[DATA_REGISTRY]


class ZoneRegistry:
    """YAML is the source of membership. Bind when both entities exist."""

    def __init__(self) -> None:
        self.climates: dict[str, MultiZoneThermostat] = {}
        self.circuits: dict[str, CircuitSelect] = {}
        self.membership: dict[str, str] = {}
        self.pending: dict[str, str] = {}

    def climate(self, entity_id: str) -> MultiZoneThermostat | None:
        """Return a registered room climate."""
        return self.climates.get(entity_id)

    def circuit(self, entity_id: str) -> CircuitSelect | None:
        """Return a registered heating circuit."""
        return self.circuits.get(entity_id)

    def members(self, circuit_id: str) -> list[MultiZoneThermostat]:
        """Climates currently bound to this circuit, stable order."""
        ids = sorted(
            cid for cid, mid in self.membership.items() if mid == circuit_id
        )
        return [self.climates[cid] for cid in ids if cid in self.climates]

    def member_ids(self, circuit_id: str) -> list[str]:
        """Entity ids of climates bound to this circuit."""
        return [sat.entity_id for sat in self.members(circuit_id)]

    def register_climate(self, climate: MultiZoneThermostat) -> None:
        """Add a room and bind it when its circuit already exists."""
        self.climates[climate.entity_id] = climate
        master_id = climate.configured_master
        if not master_id:
            return
        if self.membership.get(climate.entity_id) == master_id:
            return
        circuit = self.circuit(master_id)
        if circuit is not None:
            self._bind(climate, circuit)
        else:
            self.pending[climate.entity_id] = master_id

    def unregister_climate(self, climate: MultiZoneThermostat) -> None:
        """Drop a room. Circuit membership is cleared."""
        entity_id = climate.entity_id
        self.climates.pop(entity_id, None)
        self.pending.pop(entity_id, None)
        self.membership.pop(entity_id, None)
        climate.bind_circuit(None)

    def register_circuit(self, circuit: CircuitSelect) -> None:
        """Add a circuit and bind rooms that were waiting for it."""
        self.circuits[circuit.entity_id] = circuit
        for climate_id, master_id in list(self.pending.items()):
            if master_id != circuit.entity_id:
                continue
            climate = self.climates.get(climate_id)
            if climate is None:
                continue
            self._bind(climate, circuit)
            self.pending.pop(climate_id, None)

    def unregister_circuit(self, circuit: CircuitSelect) -> None:
        """Drop a circuit. Rooms stay registered and run locally."""
        circuit_id = circuit.entity_id
        self.circuits.pop(circuit_id, None)
        for climate_id, master_id in list(self.membership.items()):
            if master_id != circuit_id:
                continue
            self.membership.pop(climate_id, None)
            climate = self.climates.get(climate_id)
            if climate is not None:
                climate.bind_circuit(None)
                if climate.configured_master:
                    self.pending[climate_id] = climate.configured_master

    def _bind(self, climate: MultiZoneThermostat, circuit: CircuitSelect) -> None:
        self.membership[climate.entity_id] = circuit.entity_id
        climate.bind_circuit(circuit)

    def rekey_entity(self, old_id: str, new_id: str) -> None:
        """Follow an entity_id rename for a climate or a circuit."""
        if old_id == new_id:
            return

        if circuit := self.circuits.get(old_id):
            self.circuits.pop(old_id)
            self.circuits[new_id] = circuit
            for climate_id, master_id in list(self.membership.items()):
                if master_id == old_id:
                    self.membership[climate_id] = new_id
            for climate_id, master_id in list(self.pending.items()):
                if master_id == old_id:
                    self.pending[climate_id] = new_id
            return

        if climate := self.climates.get(old_id):
            self.climates.pop(old_id)
            self.climates[new_id] = climate
            if old_id in self.membership:
                self.membership[new_id] = self.membership.pop(old_id)
            if old_id in self.pending:
                self.pending[new_id] = self.pending.pop(old_id)
