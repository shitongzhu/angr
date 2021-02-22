import weakref
from typing import Set, Optional, Any, Tuple
from collections import defaultdict
import logging

import claripy
import ailment

from ... import sim_options
from ...storage.memory_mixins import LabeledMemory
from ...errors import SimMemoryMissingError
from ...code_location import CodeLocation  # pylint:disable=unused-import
from .. import register_analysis
from ..analysis import Analysis
from ..forward_analysis import ForwardAnalysis, FunctionGraphVisitor, SingleNodeGraphVisitor
from .values import Top
from .engine_vex import SimEnginePropagatorVEX
from .engine_ail import SimEnginePropagatorAIL

_l = logging.getLogger(name=__name__)


# The base state

class PropagatorState:
    def __init__(self, arch, project=None, replacements=None, only_consts=False, prop_count=None, equivalence=None):
        self.arch = arch
        self.gpr_size = arch.bits // arch.byte_width  # size of the general-purpose registers

        # propagation count of each expression
        self._prop_count = defaultdict(int) if prop_count is None else prop_count
        self._only_consts = only_consts
        self._replacements = defaultdict(dict) if replacements is None else replacements
        self._equivalence: Set[Equivalence] = equivalence if equivalence is not None else set()

        self.project = project

    def __repr__(self):
        return "<PropagatorState>"

    def _get_weakref(self):
        return weakref.proxy(self)

    def sp_offset(self, offset: int):
        base = claripy.BVS("SpOffset", self.arch.bits, explicit_name=True)
        if offset:
            base += offset
        return base

    def extract_offset_to_sp(self, spoffset_expr: claripy.ast.Base) -> Optional[int]:
        """
        Extract the offset to the original stack pointer.

        :param spoffset_expr:   The claripy AST to parse.
        :return:                The offset to the original stack pointer, or None if `spoffset_expr` is not a supported
                                type of SpOffset expression.
        """

        if 'SpOffset' in spoffset_expr.variables:
            # Local variable
            if spoffset_expr.op == "BVS":
                return 0
            elif spoffset_expr.op == '__add__' and \
                    isinstance(spoffset_expr.args[1], claripy.ast.Base) and spoffset_expr.args[1].op == "BVV":
                return spoffset_expr.args[1].args[0]
        return None

    def top(self, size: int):
        """
        Get a TOP value.

        :param size:    Width of the TOP value (in bits).
        :return:        The TOP value.
        """

        r = claripy.BVS("TOP", size, explicit_name=True)
        return r

    def is_top(self, expr) -> bool:
        """
        Check if the given expression is a TOP value.

        :param expr:    The given expression.
        :return:        True if the expression is TOP, False otherwise.
        """
        if isinstance(expr, claripy.ast.Base):
            if expr.op == "BVS" and expr.args[0] == "TOP":
                return True
            if "TOP" in expr.variables:
                return True
        return False

    def copy(self) -> 'PropagatorState':
        raise NotImplementedError()

    def merge(self, *others):

        state = self.copy()

        for o in others:
            for loc, vars_ in o._replacements.items():
                if loc not in state._replacements:
                    state._replacements[loc] = vars_.copy()
                else:
                    for var, repl in vars_.items():
                        if var not in state._replacements[loc]:
                            state._replacements[loc][var] = repl
                        else:
                            if state._replacements[loc][var] != repl:
                                state._replacements[loc][var] = Top(self.arch.byte_width)
            state._equivalence |= o._equivalence

        return state

    def add_replacement(self, codeloc, old, new):
        """
        Add a replacement record: Replacing expression `old` with `new` at program location `codeloc`.
        If the self._only_consts flag is set to true, only constant values will be set.

        :param CodeLocation codeloc:    The code location.
        :param old:                     The expression to be replaced.
        :param new:                     The expression to replace with.
        :return:                        None
        """
        if self.is_top(new):
            return

        if self._only_consts:
            if isinstance(new, int) or type(new) is Top:
                self._replacements[codeloc][old] = new
        else:
            self._replacements[codeloc][old] = new

    def filter_replacements(self):
        pass


# VEX state

class PropagatorVEXState(PropagatorState):
    def __init__(self, arch, project=None, registers=None, local_variables=None, replacements=None, only_consts=False,
                 prop_count=None):
        super().__init__(arch, project=project, replacements=replacements, only_consts=only_consts, prop_count=prop_count)
        self._registers = LabeledMemory(memory_id='reg', top_func=self.top) if registers is None else registers
        self._stack_variables = LabeledMemory(memory_id='mem', top_func=self.top) if local_variables is None else local_variables

        self._registers.set_state(self)
        self._stack_variables.set_state(self)

    def __repr__(self):
        return "<PropagatorVEXState>"

    def copy(self) -> 'PropagatorVEXState':
        cp = PropagatorVEXState(
            self.arch,
            project=self.project,
            registers=self._registers.copy(),
            local_variables=self._stack_variables.copy(),
            replacements=self._replacements.copy(),
            prop_count=self._prop_count.copy(),
            only_consts=self._only_consts
        )

        return cp

    def merge(self, *others: 'PropagatorVEXState') -> 'PropagatorVEXState':
        state = self.copy()
        state._registers.merge([o._registers for o in others], None)
        state._stack_variables.merge([o._stack_variables for o in others], None)
        return state

    def store_local_variable(self, offset, size, value, endness):  # pylint:disable=unused-argument
        # TODO: Handle size
        self._stack_variables.store(offset, value, size=size, endness=endness)

    def load_local_variable(self, offset, size, endness):  # pylint:disable=unused-argument
        # TODO: Handle size
        try:
            return self._stack_variables.load(offset, size=size, endness=endness)
        except SimMemoryMissingError:
            return self.top(size * self.arch.byte_width)

    def store_register(self, offset, size, value):
        self._registers.store(offset, value, size=size)

    def load_register(self, offset, size):

        # TODO: Fix me
        if size != self.gpr_size:
            return self.top(size * self.arch.byte_width)

        try:
            return self._registers.load(offset, size=size)
        except SimMemoryMissingError:
            return self.top(size * self.arch.byte_width)


# AIL state


class Equivalence:
    __slots__ = ('codeloc', 'atom0', 'atom1',)

    def __init__(self, codeloc, atom0, atom1):
        self.codeloc = codeloc
        self.atom0 = atom0
        self.atom1 = atom1

    def __repr__(self):
        return "<Eq@%r: %r==%r>" % (self.codeloc, self.atom0, self.atom1)

    def __eq__(self, other):
        return type(other) is Equivalence \
               and other.codeloc == self.codeloc \
               and other.atom0 == self.atom0 \
               and other.atom1 == self.atom1

    def __hash__(self):
        return hash((Equivalence, self.codeloc, self.atom0, self.atom1))


class PropagatorAILState(PropagatorState):
    def __init__(self, arch, project=None, replacements=None, only_consts=False, prop_count=None, equivalence=None,
                 stack_variables=None, registers=None):
        super().__init__(arch, project=project, replacements=replacements, only_consts=only_consts, prop_count=prop_count,
                         equivalence=equivalence)

        self._stack_variables = LabeledMemory(memory_id='reg', top_func=self.top) \
            if stack_variables is None else stack_variables
        self._registers = LabeledMemory(memory_id='mem', top_func=self.top) \
            if registers is None else registers
        self._tmps = {}

        self._registers.set_state(self)
        self._stack_variables.set_state(self)

    def __repr__(self):
        return "<PropagatorAILState>"

    def copy(self):
        rd = PropagatorAILState(
            self.arch,
            project=self.project,
            replacements=self._replacements.copy(),
            prop_count=self._prop_count.copy(),
            only_consts=self._only_consts,
            equivalence=self._equivalence.copy(),
            stack_variables=self._stack_variables.copy(),
            registers=self._registers.copy(),
            # drop tmps
        )

        return rd

    def merge(self, *others) -> 'PropagatorAILState':
        # TODO:
        state: 'PropagatorAILState' = super().merge(*others)

        state._registers.merge([o._registers for o in others], None)
        state._stack_variables.merge([o._stack_variables for o in others], None)
        return state

    def store_variable(self, variable, value, def_at) -> None:
        if variable is None or value is None:
            return
        if isinstance(value, ailment.Expr.Expression) and value.has_atom(variable, identity=False):
            return

        if isinstance(variable, ailment.Expr.Tmp):
            self._tmps[variable.tmp_idx] = value
        elif isinstance(variable, ailment.Expr.Register):
            if isinstance(value, claripy.ast.Base):
                # We directly store the value in memory
                expr = None
            elif isinstance(value, ailment.Expr.Expression):
                # the value is an expression. the actual value will be TOP.
                expr = value
                value = self.top(expr.bits)
            else:
                raise TypeError("Unsupported value type %s" % type(value))

            label = {
                'expr': expr,
                'def_at': def_at,
            }

            self._registers.store(variable.reg_offset, value, size=variable.size, label=label)
        else:
            _l.warning("Unsupported old variable type %s.", type(variable))

    def store_stack_variable(self, addr, size, new, endness=None) -> None:  # pylint:disable=unused-argument
        if isinstance(addr, ailment.Expr.StackBaseOffset):
            if addr.offset is None:
                offset = 0
            else:
                offset = addr.offset
            self._stack_variables.store(offset, new, size=size)
        else:
            _l.warning("Unsupported addr type %s.", type(addr))

    def get_variable(self, variable) -> Any:
        if isinstance(variable, ailment.Expr.Tmp):
            return self._tmps.get(variable.tmp_idx, None)
        elif isinstance(variable, ailment.Expr.Register):
            try:
                value, labels = self._registers.load_with_labels(variable.reg_offset, size=variable.size)
            except SimMemoryMissingError:
                # value does not exist
                return None

            if len(labels) == 1:
                # extract labels
                label = labels[0]
                expr = label['expr']
                def_at = label['def_at']
            else:
                # Multiple definitions and expressions
                expr = None
                def_at = None

            if self.is_top(value):
                # return an expression if there is one, or return a Top
                if expr is not None:
                    if isinstance(expr, ailment.Expr.Expression):
                        copied_expr = expr.copy()
                        copied_expr.tags['def_at'] = def_at
                        return copied_expr
                    else:
                        return expr
                if value.size() != variable.bits:
                    return self.top(variable.bits)
                return value

            # value is not TOP. ignore the expression and just return the value directly
            if value.size() != variable.bits:
                raise TypeError("Incorrect sized read. Expect %d bits." % variable.bits)

            return value

        return None

    def get_stack_variable(self, addr, size, endness=None):  # pylint:disable=unused-argument
        if isinstance(addr, ailment.Expr.StackBaseOffset):
            objs = self._stack_variables.load(addr.offset, size=size)
            if not objs:
                return None
            return next(iter(objs))
        return None

    def add_replacement(self, codeloc, old, new):

        if isinstance(new, ailment.statement.Call):
            # do not replace anything with a call expression
            return

        if type(new) is Top:
            # eliminate the past propagation of this expression
            if codeloc in self._replacements and old in self._replacements[codeloc]:
                del self._replacements[codeloc][old]
            return

        prop_count = 0
        if not isinstance(old, ailment.Expr.Tmp) and isinstance(new, ailment.Expr.Expression) \
                and not isinstance(new, ailment.Expr.Const):
            self._prop_count[new] += 1
            prop_count = self._prop_count[new]

        if prop_count <= 1:
            # we can propagate this expression
            super().add_replacement(codeloc, old, new)
        else:
            # eliminate the past propagation of this expression
            for codeloc_ in self._replacements:
                if old in self._replacements[codeloc_]:
                    del self._replacements[codeloc_][old]

    def filter_replacements(self):

        to_remove = set()

        for old, new in self._replacements.items():
            if isinstance(new, ailment.Expr.Expression) and not isinstance(new, ailment.Expr.Const):
                if self._prop_count[new] > 1:
                    # do not propagate this expression
                    to_remove.add(old)

        for old in to_remove:
            del self._replacements[old]

    def add_equivalence(self, codeloc, old, new):
        eq = Equivalence(codeloc, old, new)
        self._equivalence.add(eq)


class PropagatorAnalysis(ForwardAnalysis, Analysis):  # pylint:disable=abstract-method
    """
    PropagatorAnalysis propagates values (either constant values or variables) and expressions inside a block or across
    a function.

    PropagatorAnalysis supports both VEX and AIL. The VEX propagator only performs constant propagation. The AIL
    propagator performs both constant propagation and copy propagation of depth-N expressions.

    PropagatorAnalysis performs certain arithmetic operations between constants, including but are not limited to:

    - addition
    - subtraction
    - multiplication
    - division
    - xor

    It also performs the following memory operations:

    - Loading values from a known address
    - Writing values to a stack variable
    """

    def __init__(self, func=None, block=None, func_graph=None, base_state=None, max_iterations=3,
                 load_callback=None, stack_pointer_tracker=None, only_consts=False, completed_funcs=None):
        if func is not None:
            if block is not None:
                raise ValueError('You cannot specify both "func" and "block".')
            # traversing a function
            graph_visitor = FunctionGraphVisitor(func, func_graph)
        elif block is not None:
            # traversing a block
            graph_visitor = SingleNodeGraphVisitor(block)
        else:
            raise ValueError('Unsupported analysis target.')

        ForwardAnalysis.__init__(self, order_jobs=True, allow_merging=True, allow_widening=False,
                                 graph_visitor=graph_visitor)

        self._base_state = base_state
        self._function = func
        self._max_iterations = max_iterations
        self._load_callback = load_callback
        self._stack_pointer_tracker = stack_pointer_tracker  # only used when analyzing AIL functions
        self._only_consts = only_consts
        self._completed_funcs = completed_funcs

        self._node_iterations = defaultdict(int)
        self._states = {}
        self.replacements: Optional[defaultdict] = None
        self.equivalence: Set[Equivalence] = set()

        self._engine_vex = SimEnginePropagatorVEX(project=self.project)
        self._engine_ail = SimEnginePropagatorAIL(
            stack_pointer_tracker=self._stack_pointer_tracker,
            # We only propagate tmps within the same block. This is because the lifetime of tmps is one block only.
            propagate_tmps=block is not None,
        )

        self._analyze()

    #
    # Main analysis routines
    #

    def _pre_analysis(self):
        pass

    def _pre_job_handling(self, job):
        pass

    def _initial_abstract_state(self, node):
        if isinstance(node, ailment.Block):
            # AIL
            state = PropagatorAILState(self.project.arch, project=self.project, only_consts=self._only_consts)
        else:
            # VEX
            state = PropagatorVEXState(self.project.arch, project=self.project, only_consts=self._only_consts)
            spoffset_var = state.sp_offset(0)
            state.store_register(self.project.arch.sp_offset,
                                 self.project.arch.bytes,
                                 spoffset_var,
                                 )
        return state

    def _merge_states(self, node, *states):
        return states[0].merge(*states[1:])

    def _run_on_node(self, node, state):

        if isinstance(node, ailment.Block):
            block = node
            block_key = (node.addr, node.idx)
            engine = self._engine_ail
        else:
            block = self.project.factory.block(node.addr, node.size, opt_level=1, cross_insn_opt=False)
            block_key = node.addr
            engine = self._engine_vex
            if block.size == 0:
                # maybe the block is not decodeable
                return False, state

        state = state.copy()
        # Suppress spurious output
        if self._base_state is not None:
            self._base_state.options.add(sim_options.SYMBOL_FILL_UNCONSTRAINED_REGISTERS)
            self._base_state.options.add(sim_options.SYMBOL_FILL_UNCONSTRAINED_MEMORY)
        state = engine.process(state, block=block, project=self.project, base_state=self._base_state,
                               load_callback=self._load_callback, fail_fast=self._fail_fast)
        state.filter_replacements()

        self._node_iterations[block_key] += 1
        self._states[block_key] = state

        if self.replacements is None:
            self.replacements = state._replacements
        else:
            self.replacements.update(state._replacements)

        self.equivalence |= state._equivalence

        # TODO: Clear registers according to calling conventions

        if self._node_iterations[block_key] < self._max_iterations:
            return True, state
        else:
            return False, state

    def _intra_analysis(self):
        pass

    def _check_func_complete(self, func):
        """
        Checks if a function is completely created by the CFG. Completed
        functions are passed to the Propagator at initialization. Defaults to
        being empty if no pass is initiated.

        :param func:    Function to check (knowledge_plugins.functions.function.Function)
        :return:        Bool
        """
        complete = False
        if self._completed_funcs is None:
            return complete

        if func.addr in self._completed_funcs:
            complete = True

        return complete

    def _post_analysis(self):
        """
        Post Analysis of Propagation().
        We add the current propagation replacements result to the kb if the
        function has already been completed in cfg creation.
        """
        if self._function is not None:
            if self._check_func_complete(self._function):
                func_loc = CodeLocation(self._function.addr, None)
                self.kb.propagations.update(func_loc, self.replacements)

    def _check_prop_kb(self):
        """
        Checks, and gets, stored propagations from the KB for the current
        Propagation state.

        :return:    None or Dict of replacements
        """
        replacements = None
        if self._function is not None:
            func_loc = CodeLocation(self._function.addr, None)
            replacements = self.kb.propagations.get(func_loc)

        return replacements

    def _analyze(self):
        """
        The main analysis for Propagator. Overwritten to include an optimization to stop
        analysis if we have already analyzed the entire function once.
        """
        self._pre_analysis()

        # optimization check
        stored_replacements = self._check_prop_kb()
        if stored_replacements is not None:
            if self.replacements is not None:
                self.replacements.update(stored_replacements)
            else:
                self.replacements = stored_replacements

        # normal analysis execution
        elif self._graph_visitor is None:
            # There is no base graph that we can rely on. The analysis itself should generate successors for the
            # current job.
            # An example is the CFG recovery.

            self._analysis_core_baremetal()

        else:
            # We have a base graph to follow. Just handle the current job.

            self._analysis_core_graph()

        self._post_analysis()


register_analysis(PropagatorAnalysis, "Propagator")
