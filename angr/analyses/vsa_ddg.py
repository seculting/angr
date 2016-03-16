import logging
from collections import defaultdict

import networkx

from simuvex import SimRegisterVariable, SimMemoryVariable

from ..errors import AngrDDGError
from ..analysis import Analysis, register_analysis
from .code_location import CodeLocation

l = logging.getLogger(name="angr.analyses.vsa_ddg")

class DefUseChain(object):
    """
    Stand for a def-use chain. it is generated by the DDG itself.
    """

    def __init__(self, def_loc, use_loc, variable):
        """
        Constructor.

        :param def_loc:
        :param use_loc:
        :param variable:
        :return:
        """
        self.def_loc = def_loc
        self.use_loc = use_loc
        self.variable = variable

class VSA_DDG(Analysis):
    """
    A Data dependency graph based on VSA states.
    That means we don't (and shouldn't) expect any symbolic expressions.
    """

    def __init__(self,
                 vfg=None,
                 start_addr=None,
                 interfunction_level=0,
                 context_sensitivity_level=2,
                 keep_data=False,

                 ):
        """
        Constructor.

        :param vfg:                 An already constructed VFG. If not specified, a new VFG will be created with other
                                    specified parameters. `vfg` and `start_addr` cannot both be unspecified.
        :param start_addr:          The address where to start the analysis (typically, a function's entry point).
        :param interfunction_level: See VFG analysis.
        :param context_sensitivity_level: See VFG analysis.
        :param keep_data:           Whether we keep set of addresses as edges in the graph, or just the cardinality of
                                    the sets, which can be used as a "weight".
        """

        # sanity check
        if vfg is None and start_addr is None:
            raise AngrDDGError('Argument vfg and start_addr cannot both be unspecified.')

        if vfg is not None:
            self._vfg = vfg
        else:
            self._vfg = self.project.analyses.VFG(function_start=start_addr,
                                             interfunction_level=interfunction_level,
                                             context_sensitivity_level=context_sensitivity_level)

        self.graph = networkx.DiGraph()
        self.keep_data = keep_data

        self._simproc_map = {}
        self._imarks = {}

        self._explore()

    #
    # Properties
    #

    def __contains__(self, code_location):
        """
        If `code_location` is in the graph.

        :param code_location:   A CodeLocation instance.
        :returns:               True/False.
        """

        return code_location in self.graph

    #
    # Public methods
    #

    def get_predecessors(self, code_location):
        """
        Returns all predecessors of `code_location`.

        :param code_location:   A CodeLocation instance.
        :returns:               A list of all predecessors.
        """

        return self.graph.predecessors(code_location)

    #
    # Private methods
    #

    def _explore(self):
        """
        Starting from the start_node, explore the entire VFG, and perform the following:
        - Generate def-use chains for all registers and memory addresses using a worklist
        """

        # TODO: The worklist algorithm can definitely use some optimizations. It is a future work.

        # The worklist holds individual VFGNodes that comes from the VFG
        # Initialize the worklist with all nodes in VFG
        worklist = list(self._vfg.graph.nodes_iter())
        # Set up a set of worklist for fast inclusion test
        worklist_set = set(worklist)

        # A dict storing defs set
        # variable -> locations
        live_defs_per_node = { }

        while worklist:
            # Pop out a node
            node = worklist[0]
            worklist_set.remove(node)
            worklist = worklist[ 1 : ]

            # Grab all final states. There are usually more than one (one state for each successor), and we gotta
            # process all of them
            final_states = node.final_states

            if node in live_defs_per_node:
                live_defs = live_defs_per_node[node]
            else:
                live_defs = { }
                live_defs_per_node[node] = live_defs

            successing_nodes = self._vfg.graph.successors(node)
            for state in final_states:
                if state.scratch.jumpkind == 'Ijk_FakeRet' and len(final_states) > 1:
                    # Skip fakerets if there are other control flow transitions available
                    continue

                # TODO: Match the jumpkind
                # TODO: Support cases where IP is undecidable
                corresponding_successors = [ n for n in successing_nodes if n.addr == state.se.any_int(state.ip) ]
                if not corresponding_successors:
                    continue
                successing_node = corresponding_successors[0]

                new_defs = self._track(state, live_defs)

                if successing_node in live_defs_per_node:
                    defs_for_next_node = live_defs_per_node[successing_node]
                else:
                    defs_for_next_node = { }
                    live_defs_per_node[successing_node] = defs_for_next_node

                changed = False
                for var, code_loc_set in new_defs.iteritems():
                    if var not in defs_for_next_node:
                        defs_for_next_node[var] = code_loc_set
                        changed = True

                    else:
                        for code_loc in code_loc_set:
                            if code_loc not in defs_for_next_node[var]:
                                defs_for_next_node[var].add(code_loc)
                                changed = True

                if changed:
                    # Put all reachable successors back to our worklist again
                    if successing_node not in worklist_set:
                        worklist.append(successing_node)
                        worklist_set.add(successing_node)
                    all_successors_dict = networkx.dfs_successors(self._vfg.graph, source=successing_node)
                    for successors in all_successors_dict.values():
                        for s in successors:
                            if s not in worklist_set:
                                worklist.append(s)
                                worklist_set.add(s)

    def _track(self, state, live_defs):
        """
        Given all live definitions prior to this program point, track the changes, and return a new list of live
        definitions. We scan through the action list of the new state to track the changes.

        :param state:       The input state at that program point.
        :param live_defs:   A list of all live definitions prior to reaching this program point.
        :returns:           A list of new live definitions.
        """

        # Make a copy of live_defs
        live_defs = live_defs.copy()

        action_list = list(state.log.actions)

        # Since all temporary variables are local, we simply track them in a local dict
        temps = { }

        # All dependence edges are added to the graph either at the end of this method, or when they are going to be
        # overwritten by a new edge. This is because we sometimes have to modify a  previous edge (e.g. add new labels
        # to the edge)
        temps_to_edges = defaultdict(list)
        regs_to_edges = defaultdict(list)

        def _annotate_edges_in_dict(dict_, key, **new_labels):
            """

            :param dict_:       The dict, can be either `temps_to_edges` or `regs_to_edges`
            :param key:         The key used in finding elements in the dict
            :param new_labels:  New labels to be added to those edges
            """

            for edge_tuple in dict_[key]:
                # unpack it
                _, _, labels = edge_tuple
                for k, v in new_labels.iteritems():
                    if k in labels:
                        labels[k] = labels[k] + (v, )
                    else:
                        # Construct a tuple
                        labels[k] = (v, )

        def _dump_edge_from_dict(dict_, key, del_key=True):
            """
            Pick an edge from the dict based on the key specified, add it to our graph, and remove the key from dict.

            :param dict_:   The dict, can be either `temps_to_edges` or `regs_to_edges`.
            :param key:     The key used in finding elements in the dict.
            """
            for edge_tuple in dict_[key]:
                # unpack it
                prev_code_loc, current_code_loc, labels = edge_tuple
                # Add the new edge
                self._add_edge(prev_code_loc, current_code_loc, **labels)

            # Clear it
            if del_key:
                del dict_[key]

        for a in action_list:

            if a.bbl_addr is None:
                current_code_loc = CodeLocation(None, None, sim_procedure=a.sim_procedure)
            else:
                current_code_loc = CodeLocation(a.bbl_addr, a.stmt_idx, ins_addr=a.ins_addr)

            if a.type == "mem":
                if a.actual_addrs is None:
                    # For now, mem reads don't necessarily have actual_addrs set properly
                    addr_list = set(state.memory.normalize_address(a.addr.ast, convert_to_valueset=True))
                else:
                    addr_list = set(a.actual_addrs)

                for addr in addr_list:
                    variable = SimMemoryVariable(addr, a.data.ast.size()) # TODO: Properly unpack the SAO

                    if a.action == "read":
                        # Create an edge between def site and use site

                        prevdefs = self._def_lookup(live_defs, variable)

                        for prev_code_loc, labels in prevdefs.iteritems():
                            self._read_edge = True
                            self._add_edge(prev_code_loc, current_code_loc, **labels)

                    else: #if a.action == "write":
                        # Kill the existing live def
                        self._kill(live_defs, variable, current_code_loc)

                    # For each of its register dependency and data dependency, we revise the corresponding edge
                    for reg_off in a.addr.reg_deps:
                        _annotate_edges_in_dict(regs_to_edges, reg_off, subtype='mem_addr')
                    for tmp in a.addr.tmp_deps:
                        _annotate_edges_in_dict(temps_to_edges, tmp, subtype='mem_addr')

                    for reg_off in a.data.reg_deps:
                        _annotate_edges_in_dict(regs_to_edges, reg_off, subtype='mem_data')
                    for tmp in a.data.tmp_deps:
                        _annotate_edges_in_dict(temps_to_edges, tmp, subtype='mem_data')

            elif a.type == 'reg':
                # For now, we assume a.offset is not symbolic
                # TODO: Support symbolic register offsets

                #variable = SimRegisterVariable(a.offset, a.data.ast.size())
                variable = SimRegisterVariable(a.offset, self.project.arch.bits)

                if a.action == 'read':
                    # What do we want to do?
                    prevdefs = self._def_lookup(live_defs, variable)

                    if a.offset in regs_to_edges:
                        _dump_edge_from_dict(regs_to_edges, a.offset)

                    for prev_code_loc, labels in prevdefs.iteritems():
                        edge_tuple = (prev_code_loc, current_code_loc, labels)
                        regs_to_edges[a.offset].append(edge_tuple)

                else:
                    # write
                    self._kill(live_defs, variable, current_code_loc)

            elif a.type == 'tmp':
                # tmp is definitely not symbolic
                if a.action == 'read':
                    prev_code_loc = temps[a.tmp]
                    edge_tuple = (prev_code_loc, current_code_loc, {'type':'tmp', 'data':a.tmp})

                    if a.tmp in temps_to_edges:
                        _dump_edge_from_dict(temps_to_edges, a.tmp)

                    temps_to_edges[a.tmp].append(edge_tuple)

                else:
                    # write
                    temps[a.tmp] = current_code_loc

            elif a.type == 'exit':
                # exits should only depend on tmps

                for tmp in a.tmp_deps:
                    prev_code_loc = temps[tmp]
                    edge_tuple = (prev_code_loc, current_code_loc, {'type': 'exit', 'data': tmp})

                    if tmp in temps_to_edges:
                        _dump_edge_from_dict(temps_to_edges, tmp)

                    temps_to_edges[tmp].append(edge_tuple)

        # In the end, dump all other edges in those two dicts
        for reg_offset in regs_to_edges:
            _dump_edge_from_dict(regs_to_edges, reg_offset, del_key=False)
        for tmp in temps_to_edges:
            _dump_edge_from_dict(temps_to_edges, tmp, del_key=False)

        return live_defs

    # TODO : This docstring is out of date, what is addr_list?
    def _def_lookup(self, live_defs, variable):
        """
        This is a backward lookup in the previous defs.

        :param addr_list: a list of normalized addresses.
                        Note that, as we are using VSA, it is possible that @a is affected by several definitions.
        :returns:        a dict {stmt:labels} where label is the number of individual addresses of addr_list (or the
                        actual set of addresses depending on the keep_addrs flag) that are definted by stmt.
        """

        prevdefs = { }

        if variable in live_defs:
            code_loc_set = live_defs[variable]
            for code_loc in code_loc_set:
                # Label edges with cardinality or actual sets of addresses
                if isinstance(variable, SimMemoryVariable):
                    type_ = 'mem'
                elif isinstance(variable, SimRegisterVariable):
                    type_ = 'reg'
                else:
                    raise AngrDDGError('Unknown variable type %s' % type(variable))

                if self.keep_data is True:
                    data = variable

                    prevdefs[code_loc] = {
                        'type': type_,
                        'data': data
                    }

                else:
                    if code_loc in prevdefs:
                        count = prevdefs[code_loc]['count'] + 1
                    else:
                        count = 0
                    prevdefs[code_loc] = {
                        'type': type_,
                        'count': count
                    }
        return prevdefs

    def _kill(self, live_defs, variable, code_loc):
        """
        Kill previous defs. `addr_list` is a list of normalized addresses.
        """

        # Case 1: address perfectly match, we kill
        # Case 2: a is a subset of the original address
        # Case 3: a is a superset of the original address

        live_defs[variable] = { code_loc }
        l.debug("XX CodeLoc %s kills variable %s", code_loc, variable)

    def _add_edge(self, s_a, s_b, **edge_labels):
        """
        Add an edge in the graph from `s_a` to statement `s_b`, where `s_a` and `s_b` are tuples of statements of the
        form (irsb_addr, stmt_idx).
        """
        # Is that edge already in the graph ?
        # If at least one is new, then we are not redoing the same path again
        if (s_a, s_b) not in self.graph.edges():
            self.graph.add_edge(s_a, s_b, **edge_labels)
            self._new = True
            l.info("New edge: %s --> %s", s_a, s_b)

    def get_all_nodes(self, simrun_addr, stmt_idx):
        """
        Get all DDG nodes matching the given basic block address and statement index.
        """
        nodes=[]
        for n in self.graph.nodes():
            if n.simrun_addr == simrun_addr and n.stmt_idx == stmt_idx:
                nodes.add(n)
        return nodes

register_analysis(VSA_DDG, 'VSA_DDG')
