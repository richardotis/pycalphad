import warnings
from collections import OrderedDict, Counter, defaultdict
from collections.abc import Mapping
from pycalphad.property_framework.computed_property import DotDerivativeComputedProperty
import pycalphad.variables as v
from pycalphad.core.utils import unpack_components, unpack_condition, unpack_phases, filter_phases, instantiate_models
from pycalphad import calculate
from pycalphad.core.errors import ConditionError
from pycalphad.core.starting_point import starting_point
from pycalphad.codegen.callables import PhaseRecordFactory
from pycalphad.core.eqsolver import _solve_eq_at_conditions
from pycalphad.core.composition_set import CompositionSet
from pycalphad.core.solver import Solver, SolverBase
from pycalphad.core.light_dataset import LightDataset
from pycalphad.model import Model
import numpy as np
from typing import Optional, Tuple
from pycalphad.io.database import Database
from pycalphad.variables import Species, StateVariable
from pycalphad.property_framework import ComputableProperty, as_property
from pycalphad.property_framework.units import unit_conversion_context, ureg, Q_
from runtype import isa
from runtype.pytypes import Dict, List, Sequence, SumType, Mapping, NoneType



def _adjust_conditions(conds) -> 'OrderedDict[StateVariable, List[float]]':
    "Adjust conditions values to be in the base units of the quantity, and within the numerical limit of the solver."
    new_conds = OrderedDict()
    minimum_composition = 1e-10
    for key, value in sorted(conds.items(), key=str):
        if key == str(key):
            key = getattr(v, key, key)
        if isinstance(key, v.MoleFraction):
            vals = unpack_condition(value)
            # "Zero" composition is a common pattern. Do not warn for that case.
            if np.any(np.logical_and(np.asarray(vals) < minimum_composition, np.asarray(vals) > 0)):
                warnings.warn(
                    f"Some specified compositions are below the minimum allowed composition of {minimum_composition}.")
            new_conds[key] = [max(val, minimum_composition) for val in vals]
        else:
            new_conds[key] = unpack_condition(value)
        if getattr(key, 'display_units', '') != '':
            new_conds[key] = Q_(new_conds[key], units=key.display_units).to(key.base_units).magnitude
    return new_conds

class SpeciesList:
    @classmethod
    def cast_from(cls, s: Sequence) -> "SpeciesList":
        return sorted(Species.cast_from(x) for x in s)

class PhaseList:
    @classmethod
    def cast_from(cls, s: SumType([str, Sequence[str]])) -> "PhaseList":
        if isinstance(s, str):
            s = [s]
        return sorted(PhaseName.cast_from(x) for x in s)

class PhaseName:
    @classmethod
    def cast_from(cls, s: str) -> "PhaseName":
        return s.upper()

class ConditionValue:
    @classmethod
    def cast_from(cls, value: SumType([float, Sequence[float]])) -> "ConditionValue":
        return unpack_condition(value)

class ConditionKey:
    @classmethod
    def cast_from(cls, key: SumType([str, StateVariable])) -> "ConditionKey":
        return as_property(key)

class TypedField:
    def __init__(self, default_factory=None, dependsOn=None):
        self.default_factory = default_factory
        self.dependsOn = dependsOn

    def __set_name__(self, owner, name):
        self.type = owner.__annotations__.get(name, None)
        self.public_name = name
        self.private_name = '_' + name
        if self.dependsOn is not None:
            for dependency in self.dependsOn:
                owner._callbacks[dependency].append(self.on_dependency_update)

    def __set__(self, obj, value):
        if (self.type != NoneType) and not isa(value, self.type) and value is not None:
            try:
                value = self.type.cast_from(value)
            except TypeError as e:
                raise e
        elif value is None and self.default_factory is not None:
            value = self.default_factory(obj)
        oldval = getattr(obj, self.private_name, None)
        setattr(obj, self.private_name, value)
        for cb in obj._callbacks[self.public_name]:
            cb(obj, self.public_name, oldval, value)

    def __get__(self, obj, objtype=None):
        if not hasattr(obj, self.private_name):
            if self.default_factory is not None:
                default_value = self.default_factory(obj)
                setattr(obj, self.private_name, default_value)
        return getattr(obj, self.private_name)

    def on_dependency_update(self, obj, updated_attribute, old_val, new_val):
        pass

class ComponentsField(TypedField):
    def __init__(self, dependsOn=None):
        super().__init__(default_factory=lambda obj: unpack_components(obj.dbf, sorted(x.name for x in obj.dbf.species)),
                         dependsOn=dependsOn)
    def __set__(self, obj, value):
        comps = sorted(unpack_components(obj.dbf, value))
        self.last_user_specified = comps
        super().__set__(obj, comps)
    def on_dependency_update(self, obj, updated_attribute, old_val, new_val):
        if updated_attribute == 'dbf':
            if not hasattr(self, 'last_user_specified'):
                comps = sorted(unpack_components(obj.dbf, self.default_factory(obj)))
            else:
                comps = sorted(unpack_components(obj.dbf, self.last_user_specified))
            self.__set__(obj, comps)

class PhasesField(TypedField):
    def __init__(self, dependsOn=None):
        super().__init__(default_factory=lambda obj: filter_phases(obj.dbf, obj.comps),
                         dependsOn=dependsOn)
    def __set__(self, obj, value):
        phases = sorted(unpack_phases(value))
        super().__set__(obj, phases)

    def __get__(self, obj, objtype=None):
        getobj = super().__get__(obj, objtype=objtype)
        return filter_phases(obj.dbf, obj.comps, getobj)

class DictField(TypedField):
    def get_proxy(self, obj):
        class DictProxy:
            @staticmethod
            def unwrap():
                return TypedField.__get__(self, obj)
            def __getattr__(pxy, name):
                getobj = TypedField.__get__(self, obj)
                if getobj == pxy:
                    raise ValueError('Proxy object points to itself')
                return getattr(getobj, name)
            def __getitem__(pxy, item):
                return TypedField.__get__(self, obj).get(item)
            def __iter__(pxy):
                return TypedField.__get__(self, obj).__iter__()
            def __setitem__(pxy, item, value):
                conds = TypedField.__get__(self, obj)
                conds[item] = value
                self.__set__(obj, conds)
            def __len__(pxy):
                return len(TypedField.__get__(self, obj))
            def __repr__(pxy):
                return repr(TypedField.__get__(self, obj))
        return DictProxy()

    def __get__(self, obj, objtype=None):
        return self.get_proxy(obj)

class ConditionsField(DictField):
    def __set__(self, obj, value):
        conditions = value.copy()
        # Temporary solution until constraint system improves
        if conditions.get(v.N) is None:
            conditions[v.N] = 1
        if np.any(np.array(conditions[v.N]) != 1):
            raise ConditionError('N!=1 is not yet supported, got N={}'.format(conditions[v.N]))
        # Modify conditions values to be within numerical limits, e.g., X(AL)=0
        # Also wrap single-valued conditions with lists
        conds = _adjust_conditions(conditions)

        for cond in conds.keys():
            if isinstance(cond, (v.MoleFraction, v.ChemicalPotential)) and cond.species not in obj.comps:
                raise ConditionError('{} refers to non-existent component'.format(cond))
        super().__set__(obj, conds)

class ModelsField(DictField):
    def __init__(self, dependsOn=None):
        super().__init__(default_factory=lambda obj: instantiate_models(obj.dbf, obj.comps, obj.phases,
                                                                        model=None, parameters=obj.parameters),
                         dependsOn=dependsOn)
    def __set__(self, obj, value):
        # Unwrap proxy objects before being stored
        if hasattr(value, 'unwrap'):
            value = value.unwrap()
        try:
            super().__set__(obj, value)
        except AttributeError:
            super().__set__(obj, None)

    def on_dependency_update(self, obj, updated_attribute, old_val, new_val):
        self.__set__(obj, self.default_factory(obj))

class PRFField(TypedField):
    def __init__(self, dependsOn=None):
        def make_prf(obj):
            try:
                prf = PhaseRecordFactory(obj.dbf, obj.comps, obj.conditions, obj.models, parameters=obj.parameters)
                prf.param_values[:] = list(obj.parameters.values())
                return prf
            except AttributeError:
                return None
        super().__init__(default_factory=make_prf, dependsOn=dependsOn)
    def on_dependency_update(self, obj, updated_attribute, old_val, new_val):
        self.__set__(obj, self.default_factory(obj))

class SolverField(TypedField):
    def on_dependency_update(self, obj, updated_attribute, old_val, new_val):
        self.__set__(obj, self.default_factory(obj))

class EquilibriumCalculationField(TypedField):
    def __get__(self, obj, objtype=None):
        if (not hasattr(obj, self.private_name)) or (getattr(obj, self.private_name) is None):
            try:
                default_value = obj.recompute()
            except AttributeError:
                default_value = None
            setattr(obj, self.private_name, default_value)
        return getattr(obj, self.private_name)

    def on_dependency_update(self, obj, updated_attribute, old_val, new_val):
        self.__set__(obj, None)


class Workspace:
    _callbacks = defaultdict(lambda: [])
    dbf: Database = TypedField(lambda _: None)
    comps: SpeciesList = ComponentsField(dependsOn=['dbf'])
    phases: PhaseList = PhasesField(dependsOn=['dbf', 'comps'])
    conditions: Mapping[ConditionKey, ConditionValue] = ConditionsField()
    verbose: bool = TypedField(lambda _: False)
    models: Mapping[PhaseName, Model] = ModelsField(dependsOn=['phases'])
    parameters: SumType([NoneType, Dict]) = DictField(lambda _: OrderedDict())
    phase_record_factory: Optional[PhaseRecordFactory] = PRFField(dependsOn=['phases', 'conditions', 'models', 'parameters'])
    calc_opts: SumType([NoneType, Dict]) = DictField(lambda _: OrderedDict())
    solver: SolverBase = SolverField(lambda obj: Solver(verbose=obj.verbose), dependsOn=['verbose'])
    eq: Optional[LightDataset] = EquilibriumCalculationField(dependsOn=['phase_record_factory', 'calc_opts', 'solver'])

    def __init__(self, *args, **kwargs):
        # Assume positional arguments are specified in class typed-attribute definition order
        for arg, attrname in zip(args, ['dbf', 'comps', 'phases', 'conditions']):
            setattr(self, attrname, arg)
        attributes = list(self.__annotations__.keys())
        for kwarg_name, kwarg_val in kwargs.items():
            if kwarg_name not in attributes:
                raise ValueError(f'{kwarg_name} is not a Workspace attribute')
            setattr(self, kwarg_name, kwarg_val)

    def recompute(self):
        str_conds = OrderedDict((str(key), value) for key, value in self.conditions.items())
        components = [x for x in sorted(self.comps)]
        desired_active_pure_elements = [list(x.constituents.keys()) for x in components]
        desired_active_pure_elements = [el.upper() for constituents in desired_active_pure_elements for el in constituents]
        pure_elements = sorted(set([x for x in desired_active_pure_elements if x != 'VA']))

        state_variables = self.phase_record_factory.state_variables

        # 'calculate' accepts conditions through its keyword arguments
        grid_opts = self.calc_opts.copy()
        statevar_strings = [str(x) for x in state_variables]
        grid_opts.update({key: value for key, value in str_conds.items() if key in statevar_strings})

        if 'pdens' not in grid_opts:
            grid_opts['pdens'] = 60

        grid = calculate(self.dbf, self.comps, self.phases, model=self.models.unwrap(), fake_points=True,
                        phase_records=self.phase_record_factory, output='GM', parameters=self.parameters.unwrap(),
                        to_xarray=False, **grid_opts)
        coord_dict = str_conds.copy()
        coord_dict['vertex'] = np.arange(len(pure_elements) + 1)  # +1 is to accommodate the degenerate degree of freedom at the invariant reactions
        coord_dict['component'] = pure_elements
        properties = starting_point(self.conditions, state_variables, self.phase_record_factory, grid)
        return _solve_eq_at_conditions(properties, self.phase_record_factory, grid,
                                       list(str_conds.keys()), state_variables,
                                       self.verbose, solver=self.solver)

    def calculate_equilibrium(self):
        self.eq = self.recompute()

    def _detect_phase_multiplicity(self):
        multiplicity = {k: 0 for k in sorted(self.phase_record_factory.keys())}
        prop_GM_values = self.eq.GM
        prop_Phase_values = self.eq.Phase
        for index in np.ndindex(prop_GM_values.shape):
            cur_multiplicity = Counter()
            for phase_name in prop_Phase_values[index]:
                if phase_name == '' or phase_name == '_FAKE_':
                    continue
                cur_multiplicity[phase_name] += 1
            for key, value in cur_multiplicity.items():
                multiplicity[key] = max(multiplicity[key], value)
        return multiplicity

    def _expand_property_arguments(self, args: Sequence[ComputableProperty]):
        "Mutates args"
        multiplicity = self._detect_phase_multiplicity()
        indices_to_delete = []
        i = 0
        while i < len(args):
            if hasattr(args[i], 'phase_name') and args[i].phase_name == '*':
                indices_to_delete.append(i)
                phase_names = sorted(self.phase_record_factory.keys())
                additional_args = args[i].expand_wildcard(phase_names=phase_names)
                args.extend(additional_args)
            elif hasattr(args[i], 'species') and args[i].species == v.Species('*'):
                indices_to_delete.append(i)
                internal_to_phase = hasattr(args[i], 'sublattice_index')
                if internal_to_phase:
                    components = [x for x in self.phase_record_factory[args[i].phase_name].variables
                                  if x.sublattice_index == args[i].sublattice_index]
                else:
                    components = self.phase_record_factory[args[i].phase_name].nonvacant_elements
                additional_args = args[i].expand_wildcard(components=components)
                args.extend(additional_args)
            elif isinstance(args[i], DotDerivativeComputedProperty):
                numerator_args = [args[i].numerator]
                self._expand_property_arguments(numerator_args)
                denominator_args = [args[i].denominator]
                self._expand_property_arguments(denominator_args)
                if (len(numerator_args) > 1) or (len(denominator_args) > 1):
                    for n_arg in numerator_args:
                        for d_arg in denominator_args:
                            args.append(DotDerivativeComputedProperty(n_arg, d_arg))
                    indices_to_delete.append(i)
            else:
                # This is a concrete ComputableProperty
                if hasattr(args[i], 'phase_name') and (args[i].phase_name is not None) \
                    and not ('#' in args[i].phase_name) and multiplicity[args[i].phase_name] > 1:
                    # Miscibility gap detected; expand property into multiple composition sets
                    additional_phase_names = [args[i].phase_name+'#'+str(multi_idx+1)
                                              for multi_idx in range(multiplicity[args[i].phase_name])]
                    indices_to_delete.append(i)
                    additional_args = args[i].expand_wildcard(phase_names=additional_phase_names)
                    args.extend(additional_args)
            i += 1
        
        # Watch deletion order! Indices will change as items are deleted
        for deletion_index in reversed(indices_to_delete):
            del args[deletion_index]

    @property
    def ndim(self) -> int:
        _ndim = 0
        for cond_val in self.conditions.values():
            if len(cond_val) > 1:
                _ndim += 1
        return _ndim

    def enumerate_composition_sets(self):
        if self.eq is None:
            return
        prop_GM_values = self.eq.GM
        prop_Y_values = self.eq.Y
        prop_NP_values = self.eq.NP
        prop_Phase_values = self.eq.Phase
        conds_keys = [str(k) for k in self.eq.coords.keys() if k not in ('vertex', 'component', 'internal_dof')]
        state_variables = list(self.phase_record_factory.values())[0].state_variables
        str_state_variables = [str(k) for k in state_variables]

        for index in np.ndindex(prop_GM_values.shape):
            cur_conds = OrderedDict(zip(conds_keys,
                                        [np.asarray(self.eq.coords[b][a], dtype=np.float_)
                                        for a, b in zip(index, conds_keys)]))
            state_variable_values = [cur_conds[key] for key in str_state_variables]
            state_variable_values = np.array(state_variable_values)
            composition_sets = []
            for phase_idx, phase_name in enumerate(prop_Phase_values[index]):
                if phase_name == '' or phase_name == '_FAKE_':
                    continue
                phase_record = self.phase_record_factory[phase_name]
                sfx = prop_Y_values[index + np.index_exp[phase_idx, :phase_record.phase_dof]]
                phase_amt = prop_NP_values[index + np.index_exp[phase_idx]]
                compset = CompositionSet(phase_record)
                compset.update(sfx, phase_amt, state_variable_values)
                composition_sets.append(compset)
            yield index, composition_sets

    def get(self, *args: Tuple[ComputableProperty], values_only=True):
        if self.ndim > 1:
            raise ValueError('Dimension of calculation is greater than one')
        args = list(map(as_property, args))
        self._expand_property_arguments(args)
        arg_units = {arg: (ureg.Unit(getattr(arg, 'base_units', '')),
                           ureg.Unit(getattr(arg, 'display_units', '')))
                     for arg in args}

        arr_size = self.eq.GM.size
        results = dict()

        prop_MU_values = self.eq.MU
        conds_keys = [str(k) for k in self.eq.coords.keys() if k not in ('vertex', 'component', 'internal_dof')]
        local_index = 0

        for index, composition_sets in self.enumerate_composition_sets():
            cur_conds = OrderedDict(zip(conds_keys,
                                        [np.asarray(self.eq.coords[b][a], dtype=np.float_)
                                        for a, b in zip(index, conds_keys)]))
            chemical_potentials = prop_MU_values[index]
            
            for arg in args:
                prop_base_units, prop_display_units = arg_units[arg]
                context = unit_conversion_context(composition_sets, arg)
                if results.get(arg, None) is None:
                    results[arg] = np.zeros((arr_size,) + arg.shape)
                results[arg][local_index, :] = Q_(arg.compute_property(composition_sets, cur_conds, chemical_potentials),
                                                  prop_base_units).to(prop_display_units, context).magnitude
            local_index += 1
        
        for arg in args:
            _, prop_display_units = arg_units[arg]
            results[arg] = Q_(results[arg], prop_display_units)

        if values_only:
            return list(results.values())
        else:
            return results

    @staticmethod
    def _property_axis_label(prop: ComputableProperty) -> str:
        propname = getattr(prop, 'display_name', None)
        if propname is not None:
            result = str(propname)
            display_units = ureg.Unit(getattr(prop, 'display_units', ''))
            if str(display_units) != '':
                result += f' [{display_units:~P}]'
            return result
        else:
            return str(prop)

    def plot(self, x: ComputableProperty, *ys: Tuple[ComputableProperty], ax=None):
        import matplotlib.pyplot as plt
        ax = ax if ax is not None else plt.gca()
        x = as_property(x)
        data = self.get(x, *ys, values_only=False)
        
        for y in data.keys():
            if y == x:
                continue
            ax.plot(data[x].magnitude, data[y].magnitude, label=str(y))
            ax.set_ylabel(self._property_axis_label(y))
        ax.set_xlabel(self._property_axis_label(x))
        ax.legend()
