"""Classes that represent the state of the entire system and entities within.

These classes wrap protobufs, which are basically a fancy NamedTuple that is
generated by the `build` Makefile target. You can read more about protobufs
online, but mainly they're helpful for serializing data over the network."""

import logging
from enum import Enum
from typing import List, Dict, Optional, Union

import numpy as np
import vpython

from orbitx import orbitx_pb2 as protos
from orbitx import common

log = logging.getLogger()


# These entity fields do not change during simulation. Thus, we don't have to
# store them in a big 1D numpy array for use in scipy.solve_ivp.
_PER_ENTITY_UNCHANGING_FIELDS = [
    'name', 'mass', 'r', 'artificial', 'atmosphere_thickness',
    'atmosphere_scaling'
]

_PER_ENTITY_MUTABLE_FIELDS = [field.name for
                              field in protos.Entity.DESCRIPTOR.fields if
                              field.name not in _PER_ENTITY_UNCHANGING_FIELDS]

_FIELD_ORDERING = {name: index for index, name in
                   enumerate(_PER_ENTITY_MUTABLE_FIELDS)}

# A special field, we reference it a couple times so turn it into a symbol
# to guard against string literal typos.
_LANDED_ON = "landed_on"
assert _LANDED_ON in [field.name for field in protos.Entity.DESCRIPTOR.fields]

# Make sure this is in sync with the corresponding enum in orbitx.proto!
Navmode = Enum('Navmode', zip([  # type: ignore
    'Manual', 'CCW Prograde', 'CW Retrograde', 'Depart Reference',
    'Approach Target', 'Pro Targ Velocity', 'Anti Targ Velocity'
], protos.Navmode.values()))


class Entity:
    """A wrapper around protos.Entity.

    Example usage:
    assert Entity(protos.Entity(x=5)).x == 5
    assert Entity(protos.Entity(x=1, y=2)).pos == [1, 2]

    To add fields, or see what fields exists, please see orbitx.proto,
    specifically the "message Entity" declaration.
    """

    def __init__(self, entity: protos.Entity):
        self.proto = entity

    def __repr__(self):
        return self.proto.__repr__()

    def __str__(self):
        return self.proto.__str__()

    # These are filled in just below this class definition. These stubs are for
    # static type analysis with mypy.
    name: str
    x: float
    y: float
    vx: float
    vy: float
    r: float
    mass: float
    heading: float
    spin: float
    fuel: float
    throttle: float
    landed_on: str
    broken: bool
    artificial: bool
    atmosphere_thickness: float
    atmosphere_scaling: float

    def screen_pos(self, origin: 'Entity') -> vpython.vector:
        """The on-screen position of this entity, relative to the origin."""
        return vpython.vector(self.x - origin.x, self.y - origin.y, 0)

    @property
    def pos(self):
        return np.asarray([self.x, self.y])

    @pos.setter
    def pos(self, coord):
        self.x = coord[0]
        self.y = coord[1]

    @property
    def v(self):
        return np.asarray([self.vx, self.vy])

    @v.setter
    def v(self, coord):
        self.vx = coord[0]
        self.vy = coord[1]

    @property
    def dockable(self):
        return self.name == common.AYSE

    def landed(self) -> bool:
        """Convenient and more elegant check to see if the entity is landed."""
        return self.landed_on != ''


class _EntityView(Entity):
    """A view into a PhysicsState, very fast to create and use.
    Setting fields will update the parent PhysicsState appropriately."""
    def __init__(self, creator: 'PhysicsState', index: int):
        self._creator = creator
        self._index = index

    def __repr__(self):
        # This is actually a bit hacky. This line implies that orbitx_pb2
        # protobuf generated code can't tell the difference between an
        # orbitx_pb2.Entity and an _EntityView. Turns out, it can't! But
        # hopefully this assumption always holds.
        return repr(Entity(self))

    def __str__(self):
        return str(Entity(self))


# I feel like I should apologize before things get too crazy. Once you read
# the following module-level loop and ask "why _EntityView a janky subclass of
# Entity, and is implemented using janky array indexing into data owned by a
# PhysicsState?".
# My excuse is that I wanted a way to index into PhysicsState and get an Entity
# for ease of use and code. I found this to be a useful API that made physics
# code cleaner, but it was _too_ useful! The PhysicsState.__getitem__ method
# that implemented this indexing was so expensive and called so often that it
# was _half_ the runtime of OrbitX at high time accelerations! My solution to
# this performance issue was to optimize PhysicsState.__getitem__ to very
# return an Entity (specifically, an _EntityView) that was very fast to
# instantiate and very fast to access.
# Hence: janky array-indexing accessors is my super-optimization! 2x speedup!
for field in protos.Entity.DESCRIPTOR.fields:
    # For every field in the underlying protobuf entity, make a
    # convenient equivalent property to allow code like the following:
    # Entity(entity).heading = 5

    def entity_fget(self, name=field.name):
        return getattr(self.proto, name)

    def entity_fset(self, val, name=field.name):
        return setattr(self.proto, name, val)

    def entity_fdel(self, name=field.name):
        return delattr(self.proto, name)

    setattr(Entity, field.name, property(
        fget=entity_fget, fset=entity_fset, fdel=entity_fdel,
        doc=f"Entity proxy of the underlying field, self.proto.{field.name}"))

    def entity_view_unchanging_fget(self, name=field.name):
        return getattr(self._creator._proto_state.entities[self._index], name)

    def entity_view_unchanging_fset(self, val, name=field.name):
        return setattr(
            self._creator._proto_state.entities[self._index], name, val)

    field_n: Optional[int]
    if field.name in _PER_ENTITY_MUTABLE_FIELDS:
        field_n = _FIELD_ORDERING[field.name]
    else:
        field_n = None

    if field.cpp_type in [field.CPPTYPE_FLOAT, field.CPPTYPE_DOUBLE]:
        def entity_view_mutable_fget(self, field_n=field_n):
            return self._creator._array_rep[
                self._creator._n * field_n + self._index]

        def entity_view_mutable_fset(self, val, field_n=field_n):
            self._creator._array_rep[
                self._creator._n * field_n + self._index] = val
    elif field.cpp_type == field.CPPTYPE_BOOL:
        # Same as if it's a float, but we have to convert float -> bool.
        def entity_view_mutable_fget(self, field_n=field_n):
            return bool(
                self._creator._array_rep[
                    self._creator._n * field_n + self._index])

        def entity_view_mutable_fset(self, val, field_n=field_n):
            self._creator._array_rep[
                self._creator._n * field_n + self._index] = val
    elif field.name == _LANDED_ON:
        # Special case, we store the index of the entity we're landed on as a
        # float, but we have to convert this to an int then the name of the
        # entity.
        def entity_view_mutable_fget(self, field_n=field_n):
            entity_index = int(
                self._creator._array_rep[
                    self._creator._n * field_n + self._index])
            if entity_index == PhysicsState.NO_INDEX:
                return ''
            return self._creator._entity_names[entity_index]

        def entity_view_mutable_fset(self, val, field_n=field_n):
            assert isinstance(val, str)
            self._creator._array_rep[
                self._creator._n * field_n + self._index] = \
                self._creator._name_to_index(val)
    elif field.cpp_type == field.CPPTYPE_STRING:
        assert field.name in _PER_ENTITY_UNCHANGING_FIELDS
    else:
        raise NotImplementedError(
            "Encountered a field in the protobuf definition of Entity that "
            "is of a type we haven't handled.")

    if field.name in _PER_ENTITY_UNCHANGING_FIELDS:
        # Note there is no fdel defined. The data is owned by the PhysicalState
        # so the PhysicalState should delete data on its own time.
        setattr(_EntityView, field.name, property(
            fget=entity_view_unchanging_fget,
            fset=entity_view_unchanging_fset,
            doc=f"_EntityView proxy of unchanging field {field.name}"
        ))

    else:
        assert field.name in _PER_ENTITY_MUTABLE_FIELDS
        setattr(_EntityView, field.name, property(
            fget=entity_view_mutable_fget,
            fset=entity_view_mutable_fset,
            doc=f"_EntityView proxy of mutable field {field.name}"
        ))


class PhysicsState:
    """The physical state of the system for use in solve_ivp and elsewhere.

    The following operations are supported:

    # Construction without a y-vector, taking all data from a PhysicalState
    PhysicsState(None, protos.PhysicalState)

    # Faster Construction from a y-vector and protos.PhysicalState
    PhysicsState(ivp_solution.y, protos.PhysicalState)

    # Access of a single Entity in the PhysicsState, by index or Entity name
    my_entity: Entity = PhysicsState[0]
    my_entity: Entity = PhysicsState['Earth']

    # Iteration over all Entitys in the PhysicsState
    for entity in my_physics_state:
        print(entity.name, entity.pos)

    # Convert back to a protos.PhysicalState (this almost never happens)
    my_physics_state.as_proto()

    Example usage:
    y = PhysicsState(y_1d, physical_state)

    entity = y[0]
    y[common.HABITAT] = habitat
    scipy.solve_ivp(y.y0())

    See help(PhysicsState.__init__) for how to initialize. Basically, the `y`
    param should be None at the very start of the program, but for the program
    to have good performance, PhysicsState.__init__ should have both parameters
    filled if it's being called more than once a second while OrbitX is running
    normally.
    """

    class NoEntityError(ValueError):
        """Raised when an entity is not found."""
        pass

    # For if an entity is not landed to anything
    NO_INDEX = -1

    # The number of single-element values at the end of the y-vector.
    # Currently just SRB_TIME and TIME_ACC are appended to the end. If there
    # are more values appended to the end, increment this and follow the same
    # code for .srb_time and .time_acc
    N_SINGULAR_ELEMENTS = 2

    # Constant indices for single-element values of the y-vector.
    SRB_TIME_INDEX = -2
    TIME_ACC_INDEX = -1

    # Datatype of internal y-vector
    DTYPE = np.float64

    def __init__(self,
                 y: Optional[np.ndarray],
                 proto_state: protos.PhysicalState):
        """Collects data from proto_state and y, when y is not None.

        There are two kinds of values we care about:
        1) values that change during simulation (like position, velocity, etc)
        2) values that do not change (like mass, radius, name, etc)

        If both proto_state and y are given, 1) is taken from y and
        2) is taken from proto_state. This is a very quick operation.

        If y is None, both 1) and 2) are taken from proto_state, and a new
        y vector is generated. This is a somewhat expensive operation."""
        assert isinstance(proto_state, protos.PhysicalState)
        assert isinstance(y, np.ndarray) or y is None

        # self._proto_state will have positions, velocities, etc for all
        # entities. DO NOT USE THESE they will be stale. Use the accessors of
        # this class instead!
        self._proto_state = protos.PhysicalState()
        self._proto_state.CopyFrom(proto_state)
        self._n = len(proto_state.entities)

        self._entity_names = \
            [entity.name for entity in self._proto_state.entities]

        if y is None:
            # We rely on having an internal array representation we can refer
            # to, so we have to build up this array representation.
            y = np.empty(
                len(proto_state.entities) * len(_PER_ENTITY_MUTABLE_FIELDS)
                + self.N_SINGULAR_ELEMENTS, dtype=self.DTYPE)

            for field_name, field_n in _FIELD_ORDERING.items():
                for entity_index, entity in enumerate(proto_state.entities):
                    proto_value = getattr(entity, field_name)
                    # Internally translate string names to indices, otherwise
                    # our entire y vector will turn into a string vector oh no.
                    # Note this will convert to floats, not integer indices.
                    if field_name == _LANDED_ON:
                        proto_value = self._name_to_index(proto_value)

                    y[self._n * field_n + entity_index] = proto_value

            y[-2] = proto_state.srb_time
            y[-1] = proto_state.time_acc
            self._array_rep = y
        else:
            # Take everything except the SRB time, the last element.
            self._array_rep: np.ndarray = y.astype(self.DTYPE)
            self._proto_state.srb_time = y[self.SRB_TIME_INDEX]
            self._proto_state.time_acc = y[self.TIME_ACC_INDEX]

        assert len(self._array_rep.shape) == 1, \
            f'y is not 1D: {self._array_rep.shape()}'
        assert (self._array_rep.size - self.N_SINGULAR_ELEMENTS) % \
            len(_PER_ENTITY_MUTABLE_FIELDS) == 0, self._array_rep.size
        assert (self._array_rep.size - self.N_SINGULAR_ELEMENTS) // \
            len(_PER_ENTITY_MUTABLE_FIELDS) == len(proto_state.entities), \
            f'{self._array_rep.size} mismatches: {len(proto_state.entities)}'

        np.mod(self.Heading, 2 * np.pi, out=self.Heading)

        self._entities_with_atmospheres: List[int] = []
        for index, entity in enumerate(self._proto_state.entities):
            if entity.atmosphere_scaling != 0 and \
                    entity.atmosphere_thickness != 0:
                self._entities_with_atmospheres.append(index)

    def _y_component(self, field_name: str) -> np.ndarray:
        """Returns an n-array with the value of a component for each entity."""
        return self._array_rep[
            _FIELD_ORDERING[field_name] * self._n:
            (_FIELD_ORDERING[field_name] + 1) * self._n
        ]

    def _index_to_name(self, index: int) -> str:
        """Translates an index into the entity list to the right name."""
        i = int(index)
        return self._entity_names[i] if i != self.NO_INDEX else ''

    def _name_to_index(self, name: Optional[str]) -> int:
        """Finds the index of the entity with the given name."""
        try:
            assert name is not None
            return self._entity_names.index(name) if name != '' \
                else self.NO_INDEX
        except ValueError:
            raise self.NoEntityError(f'{name} not in entity list')

    def y0(self):
        """Returns a y-vector suitable as input for scipy.solve_ivp."""
        return self._array_rep

    def as_proto(self) -> protos.PhysicalState:
        """Creates a protos.PhysicalState view into all internal data.

        Expensive. Consider one of the other accessors, which are faster.
        For example, if you want to iterate over all elements, use __iter__
        by doing:
        for entity in my_physics_state: print(entity.name)"""
        constructed_protobuf = protos.PhysicalState()
        constructed_protobuf.CopyFrom(self._proto_state)
        for entity_data, entity in zip(self, constructed_protobuf.entities):
            (
                entity.x, entity.y, entity.vx, entity.vy,
                entity.heading, entity.spin, entity.fuel,
                entity.throttle, entity.landed_on,
                entity.broken
            ) = (
                entity_data.x, entity_data.y, entity_data.vx, entity_data.vy,
                entity_data.heading, entity_data.spin, entity_data.fuel,
                entity_data.throttle, entity_data.landed_on,
                entity_data.broken
            )

        return constructed_protobuf

    def __len__(self):
        """Implements `len(physics_state)`."""
        return self._n

    def __iter__(self):
        """Implements `for entity in physics_state:` loops."""
        for i in range(0, self._n):
            yield self.__getitem__(i)

    def __getitem__(self, index: Union[str, int]) -> Entity:
        """Returns a Entity view at a given name or index.

        Allows the following:
        entity = physics_state[2]
        entity = physics_state[common.HABITAT]
        entity.x = 5  # Propagates to physics_state.
        """
        if isinstance(index, str):
            # Turn a name-based index into an integer
            index = self._entity_names.index(index)
        i = int(index)

        return _EntityView(self, i)

    def __setitem__(self, index: Union[str, int], val: Entity):
        """Puts a Entity at a given name or index in the state.

        Allows the following:
        PhysicsState[2] = physics_entity
        PhysicsState[common.HABITAT] = physics_entity
        """
        if isinstance(val, _EntityView) and val._creator == self:
            # The _EntityView is a view into our own data, so we already have
            # the data.
            return
        if isinstance(index, str):
            # Turn a name-based index into an integer
            index = self._entity_names.index(index)
        i = int(index)

        entity = self[i]

        (
            entity.x, entity.y, entity.vx, entity.vy, entity.heading,
            entity.spin, entity.fuel, entity.throttle, entity.landed_on,
            entity.broken
        ) = (
            val.x, val.y, val.vx, val.vy, val.heading,
            val.spin, val.fuel, val.throttle, val.landed_on,
            val.broken
        )

    def __repr__(self):
        return self.as_proto().__repr__()

    def __str__(self):
        return self.as_proto().__str__()

    @property
    def timestamp(self) -> float:
        return self._proto_state.timestamp

    @timestamp.setter
    def timestamp(self, t: float):
        self._proto_state.timestamp = t

    @property
    def srb_time(self) -> float:
        return self._proto_state.srb_time

    @srb_time.setter
    def srb_time(self, val: float):
        self._proto_state.srb_time = val
        self._array_rep[self.SRB_TIME_INDEX] = val

    @property
    def parachute_deployed(self) -> bool:
        return self._proto_state.parachute_deployed

    @parachute_deployed.setter
    def parachute_deployed(self, val: bool):
        self._proto_state.parachute_deployed = val

    @property
    def X(self):
        return self._y_component('x')

    @property
    def Y(self):
        return self._y_component('y')

    @property
    def VX(self):
        return self._y_component('vx')

    @property
    def VY(self):
        return self._y_component('vy')

    @property
    def Heading(self):
        return self._y_component('heading')

    @property
    def Spin(self):
        return self._y_component('spin')

    @property
    def Fuel(self):
        return self._y_component('fuel')

    @property
    def Throttle(self):
        return self._y_component('throttle')

    @property
    def LandedOn(self) -> Dict[int, int]:
        """Returns a mapping from index to index of entity landings.

        If the 0th entity is landed on the 2nd entity, 0 -> 2 will be mapped.
        """
        landed_map = {}
        for landed, landee in enumerate(
                self._y_component('landed_on')):
            if int(landee) != self.NO_INDEX:
                landed_map[landed] = int(landee)
        return landed_map

    @property
    def Broken(self):
        return self._y_component('broken')

    @property
    def Atmospheres(self) -> List[int]:
        """Returns a list of indexes of entities that have an atmosphere."""
        return self._entities_with_atmospheres

    @property
    def time_acc(self) -> float:
        """Returns the time acceleration, e.g. 1x or 50x."""
        return self._proto_state.time_acc

    @time_acc.setter
    def time_acc(self, new_acc: float):
        self._proto_state.time_acc = new_acc
        self._array_rep[self.TIME_ACC_INDEX] = new_acc

    def craft_entity(self):
        """Convenience function, a full Entity representing the craft."""
        return self[self.craft]

    @property
    def craft(self) -> Optional[str]:
        """Returns the currently-controlled craft.
        Not actually backed by any stored field, just a calculation."""
        if common.HABITAT not in self._entity_names and \
                common.AYSE not in self._entity_names:
            return None
        if common.AYSE not in self._entity_names:
            return common.HABITAT

        hab_index = self._name_to_index(common.HABITAT)
        ayse_index = self._name_to_index(common.AYSE)
        if self._y_component('landed_on')[hab_index] == ayse_index:
            # Habitat is docked with AYSE, AYSE is active craft
            return common.AYSE
        else:
            return common.HABITAT

    def reference_entity(self):
        """Convenience function, a full Entity representing the reference."""
        return self[self._proto_state.reference]

    @property
    def reference(self) -> str:
        """Returns current reference of the physics system, shown in GUI."""
        return self._proto_state.reference

    @reference.setter
    def reference(self, name: str):
        self._proto_state.reference = name

    def target_entity(self):
        """Convenience function, a full Entity representing the target."""
        return self[self._proto_state.target]

    @property
    def target(self) -> str:
        """Returns landing/docking target, shown in GUI."""
        return self._proto_state.target

    @target.setter
    def target(self, name: str):
        self._proto_state.target = name

    @property
    def navmode(self) -> Navmode:
        return Navmode(self._proto_state.navmode)

    @navmode.setter
    def navmode(self, navmode: Navmode):
        self._proto_state.navmode = navmode.value
