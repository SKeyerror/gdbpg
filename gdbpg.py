import gdb
import gdb.types
import re
import string

# One level of tree indentation in pgprint output ("\t" for a tab)
INDENT_STRING = "    "

# Visibility options
NOT_NULL = "not_null"
HIDE_INVALID = "hide_invalid"
NEVER_SHOW = "never_show"
ALWAYS_SHOW = "always_show"

# TODO: Put these fields in a config file
PlanNodes = ['Result', 'Repeat', 'ModifyTable','Append', 'Sequence', 'Motion', 
        'AOCSScan', 'BitmapAnd', 'BitmapOr', 'Scan', 'SeqScan', 'DynamicSeqScan',
        'TableScan', 'IndexScan', 'DynamicIndexScan', 'BitmapIndexScan',
        'BitmapHeapScan', 'BitmapAppendOnlyScan', 'BitmapTableScan',
        'DynamicTableScan', 'TidScan', 'SubqueryScan', 'FunctionScan',
        'TableFunctionScan', 'ValuesScan', 'ExternalScan', 'AppendOnlyScan',
        'Join', 'NestLoop', 'MergeJoin', 'HashJoin', 'ShareInputScan',
        'Material', 'Sort', 'Agg', 'Window', 'Unique', 'Hash', 'SetOp',
                'Limit', 'DML', 'SplitUpdate', 'AssertOp', 'RowTrigger',
                'PartitionSelector' ]

# TODO: Put these fields in a config file
PathNodes = ['Path', 'AppendOnlyPath', 'AOCSPath', 'ExternalPath', 'PartitionSelectorPath',
             'IndexPath', 'BitmapHeapPath', 'BitmapAndPath', 'BitmapOrPath', 'TidPath',
             'CdbMotionPath', 'ForeignPath', 'AppendPath', 'MergeAppendPath', 'ResultPath',
             'HashPath', 'MergePath', 'MaterialPath', 'NestPath', 'JoinPath', 'UniquePath'] 

# TODO: Put these defaults config file
DEFAULT_DISPLAY_METHODS = {
    'regular_fields': 'format_regular_field',
    'node_fields': 'format_optional_node_field',
    'list_fields': 'format_optional_node_list',
    'tree_fields': 'format_optional_node_field',
    'datatype_methods': {
            'char *': 'format_string_pointer_field',
            'const char *': 'format_string_pointer_field',
            'Bitmapset *': 'format_bitmapset_field',
            'struct timeval': 'format_timeval_field',
            'NameData': 'format_namedata_field',
            'struct nameData': 'format_namedata_field',
            'struct ItemPointerData': 'format_item_pointer_data_field',
    },
    'show_hidden': False,
    'max_recursion_depth': 30
}

# TODO: generate these overrides in a yaml config file
FORMATTER_OVERRIDES = {
    'A_Expr': {
        'fields':{
            'location': {'visibility': "never_show"},
        },
    },
    'Aggref': {
        'fields':{
            'aggcollid':     {'visibility': "not_null"},
            'inputcollid':   {'visibility': "not_null"},
            'aggtranstype':  {'visibility': "not_null"},
            # PG14+: aggno/aggtransno are -1 in the parse stage; the planner
            # assigns them.  Hide when still at the sentinel.
            'aggno':         {'visibility': "hide_invalid"},
            'aggtransno':    {'visibility': "hide_invalid"},
            # PG16+: bool, defaults to false outside the presorted-input path.
            'aggpresorted':  {'visibility': "not_null"},
            'location':      {'visibility': "never_show"},
        },
    },
    'BoolExpr': {
        'fields':{
            'args': {'skip_tag': True},
            'location': {'visibility': "never_show"},
        },
    },
    'CaseWhen': {
        'fields':{
            'location': {'visibility': "never_show"},
        },
    },
    'ColumnDef': {
        'fields': {
            'identity': {
                'visibility': "not_null",
                'formatter': 'format_generated_when',
            },
            'generated': {
                'visibility': "not_null",
                'formatter': 'format_generated_when',
            },
            'collOid': {'visibility': "not_null"},
            'location': {'visibility': "never_show"},
        }
    },
    'Const': {
        'fields': {
            'consttypmod': {'visibility': "hide_invalid"},
            'constcollid': {'visibility': "not_null"},
            'location': {'visibility': "never_show"},
        },
    },
    'Constraint': {
        'fields': {
            'location': {'visibility': "never_show"},
            'conname': {'visibility': "not_null"},
            'cooked_expr': {'visibility': "not_null"},
            'generated_when': {
                'visibility': "not_null",
                'formatter': 'format_generated_when',
            },
            'indexname': {'visibility': "not_null"},
            'indexspace': {'visibility': "not_null"},
            'access_method': {'visibility': "not_null"},
            'fk_matchtype': {
                'visibility': "not_null",
                'formatter': 'format_foreign_key_matchtype',
            },
            'fk_upd_action': {
                'visibility': "not_null",
                'formatter': 'format_foreign_key_actions',
            },
            'fk_del_action': {
                'visibility': "not_null",
                'formatter': 'format_foreign_key_actions',
            },
            'old_pktable_oid': {'visibility': "not_null"},
            'trig1Oid': {'visibility': "not_null"},
            'trig2Oid': {'visibility': "not_null"},
            'trig3Oid': {'visibility': "not_null"},
            'trig4Oid': {'visibility': "not_null"},
        },
        'datatype_methods': {
         }
    },
    'CreateStmt': {
        'fields': {
            'inhRelations': {'formatter': "format_optional_oid_list"},
        }
    },
    'DefElem': {
        'fields': {
            'defnamespace': {'visibility': "not_null"},
        },
    },
    'DistinctExpr': {
        'fields': {
            'args': {'skip_tag': True},
            'opcollid': {'visibility': "not_null"},
            'inputcollid': {'visibility': "not_null"},
        },
    },
    'EquivalenceClass': {
        'fields': {
            # TODO: These fields are nice to dump recursively, but
            # they potentially have backwards references to their parents.
            # Need a way to detect this condition and stop dumping
            'ec_sources': {'formatter': 'minimal_format_node_field', },
            'ec_derives': {'formatter': 'minimal_format_node_field', },
        },
    },
    'EState': {
        'fields':{
            'es_plannedstmt': {'visibility': "never_show"},
            # TODO: These fields crash gdbpg.py
            'es_sharenode': {'visibility': "never_show"},
        },
    },
    'ExprContext': {
        'fields':{
            'ecxt_estate': {'visibility': "never_show"},
        },
    },
    'ExprState': {
        'fields':{
            # Back-pointer to the owning PlanState.  Following it dumps the
            # whole plan state again for every ExprState hanging off it
            # (qual, ps_ProjInfo->pi_state, ...), which grows exponentially
            # with max_recursion_depth.
            'parent': {'formatter': 'minimal_format_node_field'},
        },
    },
    'IndexOptInfo': {
        'fields': {
            'rel': {'formatter': 'minimal_format_node_field', }
        },
    },
    'FuncExpr': {
        'fields':{
            'args': {'skip_tag': True},
            'funccollid': {'visibility': "not_null"},
            'inputcollid': {'visibility': "not_null"},
            'location': {'visibility': "never_show"},
        },
    },
    'FuncExprState': {
        'fields':{
            # TODO: These fields crash gdbpg.py
            'fp_arg': {'visibility': "never_show"},
            'fp_datum': {'visibility': "never_show"},
            'fp_null': {'visibility': "never_show"},
        },
    },
    'GenericExprState': {
        'fields':{
            'arg': {'skip_tag': True},
        },
    },
    'IntoClause': {
        'fields':{
            'tableSpaceName': {'visibility': 'not_null'},
        },
    },
    # TODO: It would be nice to be able to recurse into memory contexts and
    #       print the tree, but need to make its own NodeFormatter in order
    #       to make its output look like a tree
    'MemoryContextData': {
        'fields':{
            'methods': {
                    'formatter': "format_pseudo_node_field",
                    'field_type': "node_field",
                },
            'parent': {'formatter': "minimal_format_memory_context_data_field"},
            'prevchild': {'formatter': "minimal_format_memory_context_data_field"},
            'firstchild': {'formatter': "minimal_format_memory_context_data_field"},
            'nextchild': {'formatter': "minimal_format_memory_context_data_field"},
        },
    },
    'AllocSetContext': {
        'fields':{
            # GPDB only.  An accounting root points at itself, so recursing
            # into it just repeats the same context until the depth limit.
            'accountingParent': {'formatter': "minimal_format_memory_context_data_field"},
        },
    },
    'NullIfExpr': {
        'fields': {
            'args': {'skip_tag': True},
            'opcollid': {'visibility': "not_null"},
            'inputcollid': {'visibility': "not_null"},
        },
    },
    'OpExpr': {
        'fields':{
            'args': {'skip_tag': True},
            'opcollid': {'visibility': "not_null"},
            'inputcollid': {'visibility': "not_null"},
            'location': {'visibility': 'never_show'},
        },
    },
    'Param': {
        'fields':{
            'paramtypmod': {'visibility': "hide_invalid"},
            'paramcollid': {'visibility': "not_null"},
            'location': {'visibility': "never_show"},
        },
    },
    # Not a Node: dumped through the pseudo-node path.  The datums/kind/
    # indexes arrays carry no length of their own -- their size comes from
    # ndatums/strategy sitting in the same struct, so they need dedicated
    # formatters instead of being shown as bare pointers.
    'PartitionBoundInfoData': {
        'fields': {
            'strategy': {'formatter': 'format_char_field'},
            'datums': {
                  'field_type': 'node_field',
                  'formatter': 'format_partition_datums_field',
                },
            'kind': {
                  'field_type': 'node_field',
                  'formatter': 'format_partition_kind_field',
                },
            'indexes': {
                  'field_type': 'node_field',
                  'formatter': 'format_partition_indexes_field',
                },
        },
    },
    'PartitionBoundSpec': {
        'fields': {
            'everyGenList': {'formatter': 'format_everyGenList_node'},
            'location': {'visibility': "never_show"},
        },
    },
    'PartitionElem': {
        'fields': {
            'location': {'visibility': "never_show"},
        },
    },
    'PartitionSpec': {
        'fields': {
            'location': {'visibility': "never_show"},
        },
    },
    'Path': {
        'fields':{
            'parent': {'formatter': 'minimal_format_node_field'},
        },
    },
    # PG16+ introduced PlaceHolderVar.phnullingrels (mirror of Var.varnullingrels)
    # to track outer-join nullability of the placeholder; PG17/18 kept phrels too.
    # Hide them when the planner left them NULL (typical for non-outer-join PHVs).
    'PlaceHolderVar': {
        'fields':{
            'phexpr':         {'skip_tag': True},
            'phrels':         {'visibility': "not_null"},
            'phnullingrels':  {'visibility': "not_null"},
            'phlevelsup':     {'visibility': "not_null"},
        },
    },
    'PlannerGlobal': {
        'fields':{
            'subroots': {'formatter': 'minimal_format_node_list'},
            # PG19: list of C strings, not of String nodes
            'subplanNames': {
                'visibility': "not_null",
                'formatter': 'format_string_list_field',
            },
        },
    },
    'PlannerInfo': {
        'fields':{
            'parent_root': {'formatter': 'minimal_format_node_field'},
            'subroots': {'formatter': 'minimal_format_node_list'},
            # TODO: Broken. Need to make dumping these fields possible
            'simple_rel_array': {'visibility': 'never_show'},
            'simple_rte_array': {'visibility': 'never_show'},
            'upper_rels': {'formatter': 'minimal_format_node_field'},
            'upper_targets': {'formatter': 'minimal_format_node_field'},
            # Lists of plain C structs (no NodeTag): only the List itself can
            # be shown, walking the cells as Nodes reads garbage tags.
            'list_cteplaninfo': {'formatter': 'minimal_format_node_field'},
            'part_schemes': {'formatter': 'minimal_format_node_field'},
            'partition_selector_candidates': {'formatter': 'minimal_format_node_field'},
            # A MemoryContext is a Node, but its guts are of no interest here
            'planner_cxt': {'formatter': 'minimal_format_memory_context_data_field'},
        },
    },
    'Query': {
        'fields':{
            'result_relation': {'visibility': 'not_null'},
            'hasAggs': {'visibility': 'not_null'},
            'hasWindowFuncs': {'visibility': 'not_null'},
            'hasSubLinks': {'visibility': 'not_null'},
            'hasDynamicFunctions': {'visibility': 'not_null'},
            'hasFuncsWithExecRestrictions': {'visibility': 'not_null'},
            'hasDistinctOn': {'visibility': 'not_null'},
            'hasRecursive': {'visibility': 'not_null'},
            'hasModifyingCTE': {'visibility': 'not_null'},
            'hasForUpdate': {'visibility': 'not_null'},
            'hasRowSecurity': {'visibility': 'not_null'},
            'canOptSelectLockingClause': {'visibility': 'not_null'},
            'isTableValueSelect': {'visibility': 'not_null'},
        },
    },
    'RangeTblEntry': {
        'fields': {
            'relid': {'visibility': "not_null"},
            'relkind': {
                        'visibility': "not_null",
                        'formatter': 'format_char_field',
                    },
            'rellockmode': {'visibility': "not_null"},
            'tablesample': {'visibility': "not_null"},
            'subquery': {'visibility': "not_null"},
            'security_barrier': {'visibility': "not_null"},
            'tablefunc': {'visibility': "not_null"},
            'ctename': {'visibility': "not_null"},
            'tcelevelsup': {'visibility': "not_null"},
            'enrname': {'visibility': "not_null"},
            'enrtuples': {'visibility': "not_null"},
            'inh': {'visibility': "not_null"},
            'requiredPerms': {'visibility': "not_null"},
            'checkAsUser': {'visibility': "not_null"},
            'selectedCols': {'visibility': "not_null"},
            'insertedCols': {'visibility': "not_null"},
            'updatedCols': {'visibility': "not_null"},
            'extraUpdatedCols': {'visibility': "not_null"},
            'eref': {'visibility': "never_show"},
        },
    },
    'RangeVar': {
        'fields': {
            'catalogname': {'visibility': "not_null"},
            'schemaname': {'visibility': "not_null"},
            'relpersistence': {'formatter': "format_char_field"},
            'location': {'visibility': "never_show"},
        },
    },
    'RelabelType': {
        'fields': {
            'arg': {'skip_tag': True},
            'resultcollid': {'visibility': "not_null"},
            'resulttypmod': {'visibility': "hide_invalid"},
            'location': {'visibility': "never_show"},
        },
    },
    'RestrictInfo': {
        'fields': {
            'parent_ec': {'formatter': 'minimal_format_node_field', },
            'scansel_cache': {'formatter': 'minimal_format_node_field', }
        },
    },
    'SubLink': {
        'fields': {
            'location': {'visibility': "never_show"},
        },
    },
    'TargetEntry': {
        'fields':{
            'expr': {'skip_tag': True},
            'resname': {'visibility': "not_null"},
            'ressortgroupref': {'visibility': "not_null"},
            'resorigtbl': {'visibility': "not_null"},
            'resorigcol': {'visibility': "not_null"},
            'resjunk': {'visibility': "not_null"},
        },
    },
    'TupleTableSlot': {
        'fields': {
            'tts_tupleDescriptor': {
                  'field_type': 'node_field',
                  'formatter': 'format_tuple_descriptor',
                },
            'PRIVATE_tts_isnull': {
                  'field_type': 'node_field',
                  'formatter': 'format_tts_isnulls',
                },
            'PRIVATE_tts_values': {
                  'field_type': 'node_field',
                  'formatter': 'format_tts_values',
                },
        },
    },
    'TypeName': {
        'fields': {
            'typeOid': {'visibility': "not_null"},
            'typemod': {'visibility': "hide_invalid"},
            'location': {'visibility': "never_show"},
        },
    },
    'Var': {
        'fields':{
            'varno':            {'formatter': "format_varno_field"},
            'vartypmod':        {'visibility': "hide_invalid"},
            'varcollid':        {'visibility': "not_null"},
            # PG16+: RT indexes of outer joins that can null this Var.  The
            # planner only allocates this Bitmapset when the Var actually sits
            # below an outer join, so a NULL pointer (common case) is hidden.
            'varnullingrels':   {'visibility': "not_null"},
            'varlevelsup':      {'visibility': "not_null"},
            # PG18+: RETURNING old/new flavor selector (VAR_RETURNING_DEFAULT=0
            # is the common case for non-RETURNING Vars).
            'varreturningtype': {'visibility': "not_null"},
            # PG14+: replaces the GP-era 'varoattno' / 'varnoold'.  When 0 the
            # Var carries no separate syntactic alias, so hide them.
            'varnosyn':         {'visibility': "not_null"},
            'varattnosyn':      {'visibility': "not_null"},
            'location':         {'visibility': "never_show"},
        },
    },
    # Plan Nodes
    'Plan': {
        'fields': {
            'extParam': {'visibility': "not_null"},
            'allParam': {'visibility': "not_null"},
            'operatorMemKB': {'visibility': "not_null"},
            'lefttree': {
                  'field_type': 'tree_field',
                  'visibility': 'not_null',
                },
            'righttree': {
                  'field_type': 'tree_field',
                  'visibility': 'not_null',
                },
            # GPDB Only:
            'motionNode': {'formatter': "minimal_format_node_field"},
            'gpmon_pkt': {'visibility': "never_show"},
        },
    },

    # GPDB Specific Plan nodes
    'Motion': {
        'fields': {
            'hashFuncs': {'visibility': "not_null"},
            'segidColIdx': {'visibility': "not_null"},
            'numSortCols': {'visibility': "not_null"},
            'sortColIdx': {'visibility': "not_null"},
            'sortOperators': {'visibility': "not_null"},
            'nullsFirst': {'visibility': "not_null"},
            'senderSliceInfo': {'visibility': "not_null"},
        },
    },

    # State Nodes
    'PlanState': {
        'fields': {
            'lefttree': {
                  'field_type': 'tree_field',
                  'visibility': 'not_null',
                  'skip_tag': True
                },
            'righttree': {
                  'field_type': 'tree_field',
                  'visibility': 'not_null',
                  'skip_tag': True
                },
            'plan': { 'formatter': 'minimal_format_node_field', },
            'state': { 'formatter': 'minimal_format_node_field', },
            'instrument': {
                  'field_type': 'node_field',
                  'formatter': 'format_pseudo_node_field',
                },
            # GPDB only:
            'gpmon_pkt': {'visibility': 'never_show'},
        },
    },
    'AggState': {
        'fields': {
            'hashiter': {
                  'field_type': 'node_field',
                  'formatter': 'format_pseudo_node_field',
                },
        },
    },

    # GPDB Specific Partition related nodes
    'PartitionBy': {
        'fields': {
            'location': {'visibility': "never_show"},
        },
    },
    'PartitionRangeItem': {
        'fields': {
            'location': {'visibility': "never_show"},
        },
    },

    # Pseudo nodes
    'tupleDesc': {
        'fields': {
            'tdtypmod': {'visibility': "hide_invalid"},
            'tdrefcount': {'visibility': "hide_invalid"},
        },
    },
    'FormData_pg_attribute': {
        'fields': {
            'attstattarget': {'visibility': "hide_invalid"},
            'attndims': {'visibility': "not_null"},
            'attcacheoff': {'visibility': "hide_invalid"},
            'atttypmod': {'visibility': "hide_invalid"},
            'attstorage': {'formatter': "format_char_field"},
            'attalign': {'formatter': "format_char_field"},
            'attinhcount': {'visibility': "not_null"},
            'attcollation': {'visibility': "not_null"},
        },
    },
    'MotionConn': {
        'fields': {
            'sndQueue': {'visibility': "never_show"},
            'unackQueue': {'visibility': "never_show"},
            'conn_info': {
                  'field_type': 'node_field',
                  'formatter': 'format_pseudo_node_field',
                },
            'ackWaitBeginTime': {
                  'field_type': 'node_field',
                  'formatter': 'format_pseudo_node_field',
                },
            'activeXmitTime': {
                  'field_type': 'node_field',
                  'formatter': 'format_pseudo_node_field',
                },
            'remoteHostAndPort': {'formatter': "format_string_pointer_field"},
            'localHostAndPort': {'formatter': "format_string_pointer_field"},
            # TODO: need a sockaddr_storage dumping function
            'peer': {
                  'field_type': 'node_field',
                  'formatter': 'format_pseudo_node_field',
                  'visibility': 'never_show',
                },
        },
    },
}


StateNodes = []
for node in PlanNodes:
    StateNodes.append(node + "State")

JoinNodes = ['NestLoop', 'MergeJoin', 'HashJoin', 'Join', 'NestLoopState',
             'MergeJoinState', 'HashJoinState', 'JoinState']

recursion_depth = 0

def format_type(t, indent=0):
    'strip the leading T_ from the node type tag'

    t = str(t)

    if t.startswith('T_'):
        t = t[2:]

    return add_indent(t, indent)

def get_base_datatype(l):
    if isinstance(l, gdb.Type):
        stripped_type = l.strip_typedefs()
    else:
        stripped_type = l.type.strip_typedefs()
    while stripped_type.code in [gdb.TYPE_CODE_PTR, gdb.TYPE_CODE_ARRAY]:
        # from: https://sourceware.org/gdb/current/onlinedocs/gdb/Types-In-Python.html
        #
        # 'For a pointer type, the target type is the type of the
        # pointed-to object. For an array type (meaning C-like
        # arrays), the target type is the type of the elements of
        # the array. For a function or method type, the target type
        # is the type of the return value. For a complex type, the
        # target type is the type of the elements. For a typedef,
        # the target type is the aliased type.'
        stripped_type = stripped_type.target()

    return stripped_type

def get_base_datatype_string(l):
    basetype = get_base_datatype(l)
    if basetype.tag == None:
        return str(basetype)
    return basetype.tag

def is_old_style_list(l):
    try:
        x = l['head']
        return True
    except:
        return False

def read_node_tag(node):
    '''read the NodeTag at the head of 'node' and return it as a string

    Returns None if the tag can't be read at all.  Note that the returned
    string is only a real tag name ("T_Var") when the memory actually holds
    a valid NodeTag: gdb renders an out-of-range enum value as a bare
    integer, so a non-Node (e.g. a C string in a List, see
    format_string_list_field) comes back as something like "1919973477"
    ('expr' read as an int32).  Feeding that to lookup_type() is what
    produced "No type named 1919973477".
    '''
    try:
        return str(cast(node, 'Node')['type'])
    except Exception:
        return None

def is_valid_node_tag(tag):
    '''True if 'tag' is a NodeTag enumerator name rather than raw garbage'''
    return (tag != None) and tag.startswith('T_')

def type_exists(type_name):
    try:
        gdb.lookup_type(type_name)
        return True
    except gdb.error:
        return False

# These tags name no struct at all -- they are Lists of scalars, and
# format_node() dispatches on the tag alone.
TAGS_WITHOUT_STRUCT = ('T_OidList', 'T_IntList', 'T_XidList')

def unknown_node_string(node):
    '''describe why 'node' can't be dumped as a Node, None if it can be

    Anything that walks a struct generically will sooner or later be handed
    a pointer that is not a Node (a List of C strings, a List of plain
    structs like PlannerInfo.part_schemes, freed memory, ...).  Describe it
    instead of letting the cast blow up the whole dump.
    '''
    if not is_node(node):
        return "<not a node: (%s) %s>" % (node.type, node)

    tag = read_node_tag(node)
    if not is_valid_node_tag(tag):
        return "<unknown NodeTag %s at %s>" % (tag, node)

    if tag in TAGS_WITHOUT_STRUCT:
        return None

    type_name = format_type(tag)
    if not type_exists(type_name):
        return "<%s at %s: no debug info for struct %s>" % (tag, node, type_name)

    return None

def as_list(lst):
    '''cast 'lst' to (List *) so that the List members can be read

    format_node() dispatches on the NodeTag while still holding a (Node *)
    -- e.g. every cell of Agg.groupingSets is an IntList reached that way.
    Reading ['length'] or ['head'] off that (Node *) fails with "There is
    no member named length".
    '''
    try:
        if get_base_datatype_string(lst) in ('List', 'struct List'):
            return lst
        return cast(lst, 'List')
    except gdb.error:
        return lst

def list_cell_scalar_member(lst):
    '''name of the ListCell union member holding the values of a scalar List

    An IntList holds signed ints; reading them out of 'oid_value' (unsigned)
    turns -1 into 4294967295.
    '''
    members = {
        'T_IntList': 'int_value',
        'T_XidList': 'xid_value',
    }
    return members.get(read_node_tag(lst), 'oid_value')

def read_list_cell_scalar(cell, member):
    try:
        return int(cell[member])
    except gdb.error:
        # xid_value only exists since PG13; older ListCells stop at oid_value
        return int(cell['oid_value'])

def list_elements(lst):
    '''yield the pointer value of every cell of 'lst' (old and new style)'''
    lst = as_list(lst)
    if is_old_style_list(lst):
        item = lst['head']
        while str(item) != '0x0':
            yield item['data']['ptr_value']
            item = item['next']
    else:
        for col in range(0, int(lst['length'])):
            yield lst['elements'][col]['ptr_value']

def format_oid_list(lst, indent=0):
    'format list containing Oid values directly (not warapped in Node)'

    # handle NULL pointer (for List we return NIL)
    if (str(lst) == '0x0'):
        return '(NIL)'

    lst = as_list(lst)
    member = list_cell_scalar_member(lst)

    # we'll collect the formatted items into a Python list
    tlist = []
    if is_old_style_list(lst):
        item = lst['head']

        # walk the list until we reach the last item
        while str(item) != '0x0':

            # get item from the list and just grab the scalar value as int
            tlist.append(read_list_cell_scalar(item['data'], member))

            # next item
            item = item['next']
    else:
        for col in range(0, int(lst['length'])):
            element = lst['elements'][col]
            tlist.append(read_list_cell_scalar(element, member))

    return add_indent(str(tlist), indent)


def format_node_list(lst, indent=0, newline=False):
    'format list containing Node values'

    # handle NULL pointer (for List we return NIL)
    if (str(lst) == '0x0'):
        return add_indent('(NULL)', indent)

    lst = as_list(lst)

    # we'll collect the formatted items into a Python list
    tlist = []

    if is_old_style_list(lst):
        item = lst['head']

        # walk the list until we reach the last item
        while str(item) != '0x0':

            # we assume the list contains Node instances, so grab a reference
            # and cast it to (Node*)
            node = cast(item['data']['ptr_value'], 'Node')

            # append the formatted Node to the result list
            tlist.append(format_node(node))

            # next item
            item = item['next']
    else:
        for col in range(0, int(lst['length'])):
            element = lst['elements'][col]
            node = cast(element['ptr_value'], 'Node')
            tlist.append(format_node(node))

    retval = str(tlist)
    if newline:
        retval = "\n".join([str(t) for t in tlist])

    return add_indent(retval, indent)


def format_char(value):
    '''convert the 'value' into a single-character string (ugly, maybe there's a better way'''

    str_val = str(value.cast(gdb.lookup_type('char')))

    # remove the quotes (start/end)
    return str_val.split(' ')[1][1:-1]


def format_bitmapset(bitmapset):
    if (str(bitmapset) == '0x0'):
        return '0x0'

    num_words = int(bitmapset['nwords'])
    retval = '0x'
    for word in reversed(range(num_words)):
        retval += '%08x' % int(bitmapset['words'][word])
    return retval


# ---
# PartitionBoundInfoData related dumpers
#
# PartitionBoundInfoData is not a Node, and its datums / kind / indexes
# members are bare pointers whose lengths live in the sibling fields
# (ndatums, strategy, nindexes).  They can therefore only be dumped with
# the whole boundinfo at hand.

def partition_bound_strategy(boundinfo):
    return format_char(boundinfo['strategy'])

def partition_bound_natts(boundinfo, natts=None):
    '''number of Datums in each datums[i] row, None if it cannot be derived

    hash: (modulus, remainder); list: 1 value per row.  For range
    partitioning the row width is the number of key columns (partnatts),
    which lives in the PartitionKey, not in the boundinfo.
    '''
    if natts != None:
        return natts

    strategy = partition_bound_strategy(boundinfo)
    if strategy == 'h':
        return 2
    if strategy == 'l':
        return 1
    return None

def partition_bound_nindexes(boundinfo):
    try:
        return int(boundinfo['nindexes'])       # PG12+
    except gdb.error:
        pass

    # PG11: the length is implicit in the strategy
    ndatums = int(boundinfo['ndatums'])
    strategy = partition_bound_strategy(boundinfo)
    if strategy == 'l':
        return ndatums
    if strategy == 'r':
        return ndatums + 1
    if strategy == 'h':
        # greatest modulus == modulus of the last (modulus, remainder) row
        if ndatums > 0 and str(boundinfo['datums'][ndatums - 1]) != '0x0':
            return int(boundinfo['datums'][ndatums - 1][0])
    return None

def format_datum_raw(datum):
    '''a Datum without type information: small values are almost certainly
    by-value integers, big ones pointers of by-reference types'''
    value = int(datum)
    if 0 <= value < 65536:
        return str(value)
    return '0x%x' % (value & ((1 << 64) - 1))

def format_partition_datum(datum, kind=None):
    if kind != None:
        kind_string = str(kind)
        if 'MINUS_INFINITY' in kind_string or kind_string == '-1':
            return 'MINUS_INFINITY'
        if 'MAXVALUE' in kind_string or kind_string == '1':
            return 'MAXVALUE'
    return format_datum_raw(datum)

def format_partition_datums(boundinfo, natts=None, indent=0):
    datums = boundinfo['datums']
    if str(datums) == '0x0':
        return add_indent('(NULL)', indent)

    ndatums = int(boundinfo['ndatums'])
    strategy = partition_bound_strategy(boundinfo)
    row_natts = partition_bound_natts(boundinfo, natts)

    kind = None
    try:
        if str(boundinfo['kind']) != '0x0':
            kind = boundinfo['kind']
    except gdb.error:
        pass

    lines = ["(strategy='%s' ndatums=%d)" % (strategy, ndatums)]
    if row_natts == None:
        lines.append(INDENT_STRING + "<range bounds: partnatts is not stored"
                     " in the boundinfo, showing 1 Datum per row;"
                     " use 'pgprint <expr> <natts>' for multi-column keys>")
        row_natts = 1

    for i in range(ndatums):
        row = datums[i]
        if str(row) == '0x0':
            lines.append(INDENT_STRING + '[%d] (NULL)' % i)
            continue

        values = []
        for col in range(row_natts):
            col_kind = None
            if kind != None and str(kind[i]) != '0x0':
                col_kind = kind[i][col]
            values.append(format_partition_datum(row[col], col_kind))
        lines.append(INDENT_STRING + '[%d] (%s)' % (i, ', '.join(values)))

    return add_indent('\n'.join(lines), indent)

def format_partition_kind(boundinfo, natts=None, indent=0):
    kind = boundinfo['kind']
    if str(kind) == '0x0':
        return add_indent('(NULL)', indent)

    ndatums = int(boundinfo['ndatums'])
    row_natts = partition_bound_natts(boundinfo, natts)
    if row_natts == None:
        row_natts = 1

    lines = ['(ndatums=%d)' % ndatums]
    for i in range(ndatums):
        row = kind[i]
        if str(row) == '0x0':
            lines.append(INDENT_STRING + '[%d] (NULL)' % i)
            continue
        values = [str(row[col]) for col in range(row_natts)]
        lines.append(INDENT_STRING + '[%d] (%s)' % (i, ', '.join(values)))

    return add_indent('\n'.join(lines), indent)

def format_partition_indexes(boundinfo, indent=0):
    indexes = boundinfo['indexes']
    if str(indexes) == '0x0':
        return add_indent('(NULL)', indent)

    nindexes = partition_bound_nindexes(boundinfo)
    if nindexes == None:
        return add_indent('<cannot determine the length of indexes[]>', indent)

    values = [str(int(indexes[i])) for i in range(nindexes)]
    return add_indent('(nindexes=%d) [%s]' % (nindexes, ', '.join(values)),
                      indent)

PARTITION_BOUND_MEMBERS = ('datums', 'kind', 'indexes')

def print_partition_bound_member(expr, natts=None):
    '''handle 'pgprint boundinfo->datums' (and ->kind / ->indexes)

    The member pointer alone is undumpable, so re-evaluate the parent
    expression and dump the member with the boundinfo's context.  Returns
    True when the expression was recognized and printed.
    '''
    m = re.match(r'^\s*(.+)(?:->|\.)\s*(%s)\s*$'
                     % '|'.join(PARTITION_BOUND_MEMBERS), expr)
    if m == None:
        return False

    try:
        parent = gdb.parse_and_eval(m.group(1))
        if get_base_datatype_string(parent) not in (
                'PartitionBoundInfoData', 'struct PartitionBoundInfoData'):
            return False
        boundinfo = cast(parent, 'PartitionBoundInfoData')
    except Exception:
        return False

    member = m.group(2)
    if member == 'datums':
        print(format_partition_datums(boundinfo, natts))
    elif member == 'kind':
        print(format_partition_kind(boundinfo, natts))
    else:
        print(format_partition_indexes(boundinfo))
    return True

def format_partition_datums_field(node, field, cast_to=None, skip_tag=False, print_null=False, indent=1):
    if str(node[field]) == '0x0':
        if print_null:
            return add_indent('[%s] (NULL)' % field, indent, True)
        return ''
    return add_indent('[%s] %s' % (field, format_partition_datums(node)),
                      indent, True)

def format_partition_kind_field(node, field, cast_to=None, skip_tag=False, print_null=False, indent=1):
    if str(node[field]) == '0x0':
        if print_null:
            return add_indent('[%s] (NULL)' % field, indent, True)
        return ''
    return add_indent('[%s] %s' % (field, format_partition_kind(node)),
                      indent, True)

def format_partition_indexes_field(node, field, cast_to=None, skip_tag=False, print_null=False, indent=1):
    if str(node[field]) == '0x0':
        if print_null:
            return add_indent('[%s] (NULL)' % field, indent, True)
        return ''
    return add_indent('[%s] %s' % (field, format_partition_indexes(node)),
                      indent, True)
#---

def format_node_array(array, start_idx, length, indent=0):

    items = []
    for i in range(start_idx, start_idx + length - 1):
        items.append(str(i) + " => " + format_node(array[i]))

    return add_indent(("\n".join(items)), indent)

def max_depth_exceeded():
    global recursion_depth
    if recursion_depth >= DEFAULT_DISPLAY_METHODS['max_recursion_depth']:
        return True
    return False

def format_node(node, indent=0):
    'format a single Node instance (only selected Node types supported)'

    # Check the recursion depth
    global recursion_depth
    if max_depth_exceeded():
        if is_node(node):
            node = cast(node, 'Node')
            return "%s %s <max_depth_exceeded>" % (format_type(node['type']), str(node))
        else:
            return "%s <max_depth_exceeded>" % str(node)

    if str(node) == '0x0':
        return add_indent('(NULL)', indent)

    # Not everything reachable from a Node is a Node.  Report it and move on
    # rather than casting to a bogus type and aborting the whole dump.
    unknown = unknown_node_string(node)
    if unknown != None:
        return add_indent(unknown, indent)

    recursion_depth += 1

    retval = ''

    if is_a(node, 'A_Const'):
        node = cast(node, 'A_Const')

        retval = format_a_const(node)

    elif is_a(node, 'List'):
        node = cast(node, 'List')

        retval = format_node_list(node, 0, True)

    elif is_a(node, 'String'):
        # PG15+ removed the Value node; String/Integer/Float/Boolean/
        # BitString are standalone structs with their own fields.
        try:
            node = cast(node, 'String')
            retval = 'String [%s]' % getchars(node['sval'])
        except gdb.error:
            node = cast(node, 'Value')
            retval = 'String [%s]' % getchars(node['val']['str'])

    elif is_a(node, 'Integer'):
        try:
            node = cast(node, 'Integer')
            retval = 'Integer [%s]' % node['ival']
        except gdb.error:
            node = cast(node, 'Value')
            retval = 'Integer [%s]' % node['val']['ival']

    elif is_a(node, 'Float'):
        try:
            node = cast(node, 'Float')
            retval = 'Float [%s]' % getchars(node['fval'])
        except gdb.error:
            node = cast(node, 'Value')
            retval = 'Float [%s]' % getchars(node['val']['str'])

    elif is_a(node, 'Boolean'):
        # PG15+ only
        node = cast(node, 'Boolean')
        retval = 'Boolean [%s]' % node['boolval']

    elif is_a(node, 'BitString'):
        try:
            node = cast(node, 'BitString')
            retval = 'BitString [%s]' % getchars(node['bsval'])
        except gdb.error:
            node = cast(node, 'Value')
            retval = 'BitString [%s]' % getchars(node['val']['str'])

    elif is_a(node, 'OidList'):
        retval = 'OidList: %s' % format_oid_list(cast(node, 'List'))

    elif is_a(node, 'IntList'):
        retval = 'IntList: %s' % format_oid_list(cast(node, 'List'))

    elif is_a(node, 'XidList'):
        retval = 'XidList: %s' % format_oid_list(cast(node, 'List'))

    elif is_pathnode(node):
        node_formatter = PlanStateFormatter(node)
        retval += node_formatter.format()

    elif is_plannode(node):
        node_formatter = PlanStateFormatter(node)
        retval += node_formatter.format()

    elif is_statenode(node):
        node_formatter = PlanStateFormatter(node)
        retval += node_formatter.format()

    else:
        node_formatter = NodeFormatter(node)
        retval += node_formatter.format()

    recursion_depth -= 1
    return add_indent(str(retval), indent)

def is_pathnode(node):
    for nodestring in PathNodes:
        if is_a(node, nodestring):
            return True

    return False

def is_plannode(node):
    for nodestring in PlanNodes:
        if is_a(node, nodestring):
            return True

    return False

def is_statenode(node):
    for nodestring in StateNodes:
        if is_a(node, nodestring):
            return True

    return False

def is_joinnode(node):
    for nodestring in JoinNodes:
        if is_a(node, nodestring):
            return True

    return False

def format_a_const(node, indent=0):
    # PG15+: a NULL constant is A_Const.isnull = true and the val union
    # is not meaningful (pre-PG15 it was a Value node of type T_Null)
    try:
        if node['isnull']:
            return add_indent("A_Const [NULL]", indent)
    except gdb.error:
        pass

    retval = "A_Const [%(val)s]" % {
        'val': format_node(node['val'].address),
        }

    return add_indent(retval, indent)

def is_a(n, t):
    '''checks that the node has type 't' (just like IsA() macro)'''

    if not is_node(n):
        return False
    n = cast(n, 'Node')

    return (str(n['type']) == ('T_' + t))

def is_xpr(l):
    try:
        x = l['xpr']
        return True
    except:
        return False

def is_node(l):
    '''return True if the value looks like a Node (its base struct starts with a NodeTag)

    Note that a Node subtype does NOT have a member named 'type': Agg starts
    with 'Plan plan', SeqScanState with 'ScanState ss', ...  Requiring
    l['type'] to resolve would send 'pgprint node' at ExecInitAgg() down the
    pseudo-node path, which dumps Agg.plan as a plain field ("[plan] <unknown
    NodeTag None at {type = T_Agg, ...}>") instead of merging the inherited
    Plan fields the way NodeFormatter.parent_node does.
    '''
    # Reject multi-level pointers (e.g. 'List **args_p' in
    # simplify_function).  gdb's field lookup auto-dereferences through
    # every pointer level, so l['type'] would succeed on a Node** too --
    # but casting such a value to Node* misreads the pointer variable's
    # own bytes as a NodeTag ("No type named <garbage>").
    try:
        t = l.type.strip_typedefs()
        if (t.code == gdb.TYPE_CODE_PTR and
                t.target().strip_typedefs().code == gdb.TYPE_CODE_PTR):
            return False
    except Exception:
        pass

    if is_xpr(l):
        return True

    try:
        # PG19 added a NodeTag at the head of struct Bitmapset (for memory
        # tracking).  That makes Bitmapset look like a Node here even though
        # it still has a dedicated inline formatter
        # (format_bitmapset_field) and must NOT be routed through the
        # complex-node-field path; otherwise visibility filters like
        # 'not_null' on Var.varnullingrels stop applying and the hex
        # spills out after the field bracket.
        try:
            tag = get_base_datatype_string(l)
        except Exception:
            tag = ''
        # Match "Bitmapset" or "struct Bitmapset" -- gdb's struct-tag
        # convention varies across versions / typedef chains.
        if tag in ('Bitmapset', 'struct Bitmapset'):
            return False
        return has_node_tag_header(l)
    except:
        return False

def has_node_tag_header(l, depth=0):
    '''True if l's base struct starts with a NodeTag

    gdb happily finds a field named 'type' at any offset and of any type, but
    only a NodeTag at offset 0 makes the value castable to Node * -- struct
    Gang for instance has a 'GangType type' member and is not a Node at all.
    '''
    try:
        # get_base_datatype() peels pointers/arrays but leaves the typedef
        # ('PlannerInfo' rather than 'struct PlannerInfo') in place
        t = get_base_datatype(l).strip_typedefs()
        if t.code != gdb.TYPE_CODE_STRUCT:
            return False

        first_field = t.values()[0]
        field_type = first_field.type
        stripped = field_type.strip_typedefs()

        if stripped.code == gdb.TYPE_CODE_ENUM:
            return (stripped.tag == 'NodeTag') or (str(field_type) == 'NodeTag')

        # Node types that embed their parent node (Scan.plan, Var.xpr, ...)
        # still start with that parent's NodeTag.
        if stripped.code == gdb.TYPE_CODE_STRUCT and depth < 4:
            return has_node_tag_header(stripped, depth + 1)
    except Exception:
        return False

    return False

def is_type(value, type_name, is_pointer):
    t = gdb.lookup_type(type_name)
    if(is_pointer):
        t = t.pointer()
    return (str(value.type) == str(t))
    # This doesn't work for list types for some reason...
    # return (gdb.types.get_basic_type(value.type) == gdb.types.get_basic_type(t))

def as_pointer(value):
    '''return 'value' in a form that can be cast to a node pointer

    'pgprint *node' hands us the struct itself, and so does every embedded
    node sub-struct (Agg.plan, ScanState.ss, ...).  Casting a struct value
    to a pointer does not dereference it, it reinterprets the first bytes of
    the struct -- the NodeTag -- as an address ("Cannot access memory at
    address 0x1b0").  Take the address instead; the node starts there.
    '''
    try:
        if (value.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT and
                value.address != None):
            return value.address
    except gdb.error:
        pass

    return value

def cast(node, type_name):
    '''wrap the gdb cast to proper node type'''

    # lookup the type with name 'type_name' and cast the node to it
    t = gdb.lookup_type(type_name)
    return as_pointer(node).cast(t.pointer())

def get_base_node_type(node):
    if is_node(node):
        tag = read_node_tag(node)
        if not is_valid_node_tag(tag):
            return None
        return format_type(tag)

    return None

def add_indent(val, indent, add_newline=False):
    retval = ''
    if add_newline == True:
        retval += '\n'

    retval += "\n".join([((INDENT_STRING * indent) + l) for l in val.split("\n")])
    return retval

def getchars(arg):
    if (str(arg) == '0x0'):
        return str(arg)

    retval = '"'

    i=0
    while arg[i] != ord("\0"):
        character = int(arg[i].cast(gdb.lookup_type("char")))
        if chr(character) in string.printable:
            retval += "%c" % chr(character)
        else:
            retval += "\\x%x" % character
        i += 1

    retval += '"'

    return retval

def get_node_fields(node):
    nodefields = [("Node", True), ("Expr", True)]
    type_name = str(node['type']).replace("T_", "")

    t = gdb.lookup_type(type_name)
    fields = []
    for v in t.values():
        for field, is_pointer in nodefields:
            if is_type(v, field, is_pointer):
                fields.append(v.name)

    return fields

def get_list_fields(node):
    listfields = [("List", True)]
    type_name = str(node['type']).replace("T_", "")

    t = gdb.lookup_type(type_name)
    fields = []
    for v in t.values():
        for field, is_pointer in listfields:
            if is_type(v, field, is_pointer):
                fields.append(v.name)
    return fields

def format_regular_field(node, field):
    return node[field]

def format_string_pointer_field(node, field):
    return getchars(node[field])

def format_char_field(node, field):
    return "'%s'" % format_char(node[field])

def format_bitmapset_field(node, field, **kwargs):
    return format_bitmapset(node[field])

def format_generated_when(node, field):
    generated_when = {
        'a': 'ATTRIBUTE_IDENTITY_ALWAYS',
        'd': 'ATTRIBUTE_IDENTITY_BY_DEFAULT',
        's': 'ATTRIBUTE_GENERATED_STORED',
    }

    fk_char = format_char(node[field])

    if generated_when.get(fk_char) != None:
        return generated_when.get(fk_char)

    return fk_char

def format_foreign_key_matchtype(node, field):
    foreign_key_matchtypes = {
        'f': 'FKCONSTR_MATCH_FULL',
        'p': 'FKCONSTR_MATCH_PARTIAL',
        's': 'FKCONSTR_MATCH_SIMPLE',
    }

    fk_char = format_char(node[field])

    if foreign_key_matchtypes.get(fk_char) != None:
        return foreign_key_matchtypes.get(fk_char)

    return fk_char


def format_foreign_key_actions(node, field):
    foreign_key_actions = {
        'a': 'FKONSTR_ACTION_NOACTION',
        'r': 'FKCONSTR_ACTION_RESTRICT',
        'c': 'FKCONSTR_ACTION_CASCADE',
        'n': 'FKONSTR_ACTION_SETNULL',
        'd': 'FKONSTR_ACTION_SETDEFAULT',
    }

    fk_char = format_char(node[field])

    if foreign_key_actions.get(fk_char) != None:
        return foreign_key_actions.get(fk_char)

    return fk_char

def format_varno_field(node, field):
    varno_type = {
        65000: "INNER_VAR",
        65001: "OUTER_VAR",
        65002: "INDEX_VAR",
    }

    varno = int(node[field])
    if varno_type.get(varno) != None:
        return varno_type.get(varno)

    return node[field]

def format_timeval_field(node, field):
    return "%s.%s" %(node[field]['tv_sec'], node[field]['tv_usec'])

def format_namedata_field(node, field):
    return getchars(node[field]['data'])

def format_item_pointer_data_field(node, field):
    ip_blkid = node[field]['ip_blkid']
    block_hi = int(ip_blkid['bi_hi'])
    block_low = int(ip_blkid['bi_lo'])

    block_id = block_hi << 16
    block_id += block_low

    return "(%s,%s)" % (block_id, node[field]['ip_posid'])

def format_optional_node_field(node, fieldname, cast_to=None, skip_tag=False, print_null=False, indent=1):
    if cast_to != None:
        node = cast(node, cast_to)

    if str(node[fieldname]) != '0x0':
        node_output = format_node(node[fieldname])

        if skip_tag == True:
            return add_indent('%s' % node_output, indent, True)
        else:
            retval = '[%s] ' % fieldname
            if node_output.count('\n') > 0:
                node_output = add_indent(node_output, 1, True)
            retval += node_output
            return add_indent(retval , indent, True)
    elif print_null == True:
        return add_indent("[%s] (NULL)" % fieldname, indent, True)

    return ''

def format_optional_node_list(node, fieldname, cast_to=None, skip_tag=False, newLine=True, print_null=False, indent=1):
    if cast_to != None:
        node = cast(node, cast_to)

    retval = ''
    indent_add = 0
    if str(node[fieldname]) != '0x0':
        if (is_a(node[fieldname], 'OidList') or is_a(node[fieldname], 'IntList')
                or is_a(node[fieldname], 'XidList')):
            return format_optional_oid_list(node, fieldname, skip_tag, newLine, print_null, indent)

        if skip_tag == False:
            retval += add_indent('[%s]' % fieldname, indent, True)
            indent_add = 1

        if newLine == True:
            retval += '\n'
            retval += '%s' % format_node_list(node[fieldname], indent + indent_add, newLine)
        else:
            retval += ' %s' % format_node_list(node[fieldname], 0, newLine)
    elif print_null == True:
        return add_indent("[%s] (NIL)" % fieldname, indent, True)

    return retval

def format_optional_oid_list(node, fieldname, skip_tag=False, newLine=False, print_null=False, indent=1):
    retval = ''
    if str(node[fieldname]) != '0x0':
        node_type = format_type(node[fieldname]['type'])
        if skip_tag == False:
            retval += '[%s] %s' % (fieldname, format_node(node[fieldname]))
        else:
            retval += format_node(node[fieldname])

        retval = add_indent(retval, indent, True)
    elif print_null == True:
        retval += add_indent("[%s] (NIL)" % fieldname, indent, True)

    return retval

def format_string_list_field(node, fieldname, cast_to=None, skip_tag=False, newLine=False, print_null=False, indent=1):
    '''format a List whose cells are plain C strings, not String nodes

    PG19's PlannerGlobal.subplanNames is such a list ("expr", "cte_1", ...).
    The generic list walker would cast each cell to Node * and read the first
    four characters of the string as a NodeTag.
    '''
    retval = ''
    if str(node[fieldname]) != '0x0':
        strings = [getchars(cast(e, 'char')) for e in list_elements(node[fieldname])]

        if skip_tag == False:
            retval += '[%s] ' % fieldname
        retval += '[%s]' % ", ".join(strings)
        retval = add_indent(retval, indent, True)
    elif print_null == True:
        retval = add_indent("[%s] (NIL)" % fieldname, indent, True)

    return retval

def format_everyGenList_node(node, fieldname, skip_tag=False, newLine=False, print_null=False, indent=1):
    retval = ''
    if str(node[fieldname]) != '0x0':
        genlist_strings = []
        if is_old_style_list(node[fieldname]):
            item = node[fieldname]['head']

            while str(item) != '0x0':

                listnode = cast(item['data']['ptr_value'], 'List')

                genlist_item = listnode['head']
                val = '['
                while str(genlist_item) != '0x0':
                    genlist_string = getchars(cast(genlist_item['data']['ptr_value'], 'char'))
                    val += genlist_string
                    # next item
                    genlist_item = genlist_item['next']
                    if str(genlist_item) != "0x0":
                       val += ' '
                val += ']'

                genlist_strings.append(val)

                # next item
                item = item['next']
        else:
            raise Exception("Tried to dump everyGenList using new style lists")

        if skip_tag == False:
            retval += '[%s]' % fieldname

        for item in genlist_strings:
            retval += add_indent(item, 1, True)

        retval = add_indent(retval, indent, True)
    elif print_null == True:
        retval += add_indent("[%s] (NIL)" % fieldname, indent, True)

    return retval

def minimal_format_node_field(node, fieldname, cast_to=None, skip_tag=False, print_null=False, indent=1):
    retval = ''
    if str(node[fieldname]) != '0x0':
        retval = add_indent("[%s] (%s)%s" % (fieldname, node[fieldname].type, node[fieldname]), indent, True)
    elif print_null == True:
        retval = add_indent("[%s] (NIL)" % fieldname, indent, True)

    return retval

def minimal_format_node_list(node, fieldname, cast_to=None, skip_tag=False, newLine=True, print_null=False, indent=1):
    retval = ''
    indent_add = 0
    if str(node[fieldname]) != '0x0':
        if skip_tag == False:
            retval += add_indent('[%s]' % fieldname, indent, True)
            indent_add = 1

        if newLine == True:
            retval += '\n'
            retval += '%s' % minimal_format_node_list_field(node, fieldname, cast_to, skip_tag, newLine, print_null, indent + indent_add)
        else:
            retval += ' %s' % minimal_format_node_list_field(node, fieldname, cast_to, skip_tag, newLine, print_null, 0)
    elif print_null == True:
        return add_indent("[%s] (NIL)" % fieldname, indent, True)

    return retval

def minimal_format_node_list_field(node, fieldname, cast_to=None, skip_tag=False, newLine=True, print_null=False, indent=1):
    'minimal format list containing Node values'
    if cast_to != None:
        lst = cast(node[fieldname], cast_to)
    else:
        lst = node[fieldname]

    # we'll collect the formatted items into a Python list
    tlist = []

    for element in list_elements(lst):
        if str(element) == '0x0':
            tlist.append('(NULL)')
            continue

        lstnode = cast(element, 'Node')
        unknown = unknown_node_string(lstnode)
        if unknown != None:
            tlist.append(unknown)
            continue

        lstnode = cast(lstnode, get_base_node_type(lstnode))
        tlist.append("(%s)%s" % (lstnode.type, lstnode))

    if newLine:
        retval = "\n".join([str(t) for t in tlist])
    else:
        retval = str(tlist)

    return add_indent(retval, indent)


def minimal_format_memory_context_data_field(node, fieldname, cast_to=None, skip_tag=False, print_null=False, indent=1):
    retval = ''
    value = node[fieldname]
    if str(value) != '0x0':
        # GPDB's AllocSetContext.accountingParent is an (AllocSet), which
        # keeps its name in the MemoryContextData header sitting at its
        # front rather than in a member of its own
        header = value
        try:
            header['name']
        except gdb.error:
            header = cast(value, 'MemoryContextData')

        retval = add_indent("[%s] (%s)%s [name=%s]" % (fieldname, value.type, value, format_string_pointer_field(header, 'name')), indent, True)
    elif print_null == True:
        retval = add_indent("[%s] (NIL)" % fieldname, indent, True)

    return retval

def format_pseudo_node_field(node, fieldname, cast_to=None, skip_tag=False, print_null=False, indent=1):
    if str(node[fieldname]) != '0x0':
        node_type = node[fieldname].type.strip_typedefs()
        formatter = NodeFormatter(node[fieldname], pseudo_node=True)
        node_output = formatter.format()

        if skip_tag == True:
            return add_indent('%s' % node_output, indent, True)
        else:
            retval = '[%s] ' % fieldname
            if node_output.count('\n') > 0:
                node_output = add_indent(node_output, 1, True)
            retval += node_output
            return add_indent(retval , indent, True)
    elif print_null == True:
        return add_indent("[%s] (NULL)" % fieldname, indent, True)

    return ''

# ---
# TupleTableSlot related dumpers
def tuple_desc_attr(desc, i):
    '''(Form_pg_attribute) of the i'th attribute of the TupleDesc 'desc'

    The layout of that array has changed twice:

      PG11 and older  'Form_pg_attribute *attrs' -- an array of pointers
      PG12 .. PG17    'FormData_pg_attribute attrs[FLEXIBLE_ARRAY_MEMBER]'
      PG18 and newer  no 'attrs' member at all -- the array sits behind the
                      variable-length compact_attrs[natts], and is reached
                      through TupleDescAttrAddress()

    Reading desc['attrs'] on PG18+ is what produced
    "[tts_tupleDescriptor] <error: There is no member named attrs.>".
    '''
    try:
        attrs = desc['attrs']
    except gdb.error:
        # TupleDescAttrAddress(): the FormData_pg_attribute array starts
        # right past the last compact_attrs element
        attrs = (desc['compact_attrs'][0].address + int(desc['natts'])).cast(
                    gdb.lookup_type('FormData_pg_attribute').pointer())
        return attrs + i

    attr = attrs[i]
    if attr.type.strip_typedefs().code == gdb.TYPE_CODE_PTR:
        return attr

    return attr.address

def format_tuple_descriptor(node, field, cast_to=None, skip_tag=False, print_null=False, indent=1):
    if str(node[field]) == '0x0':
        return '[%s] (NULL)' % field

    natts = node[field]['natts']
    retval = format_pseudo_node_field(node, field, 'tupleDesc' , skip_tag, print_null, 0)
    for col in range(0, natts):
        attr = tuple_desc_attr(node[field], col)
        formatter = LabelNodeFormatter(attr, pseudo_node=True, label='[%s] ' % (col+1))
        retval += add_indent(formatter.format(), 1, True)

    return add_indent(retval, indent, False)

def format_tts_isnulls(node, field, cast_to=None, skip_tag=False, print_null=False, indent=1):
    if str(node['tts_tupleDescriptor']) == '0x0' or str(node[field]) == '0x0':
        if print_null:
            return '[%s] (NULL)' % field
        else:
            return ''

    descr = node['tts_tupleDescriptor'].dereference()
    natts = descr['natts']

    nullmap, nullmap_bytes = get_tts_nullmap(node, field, natts)
    retlines = []
    col = 1
    for isnull, byte in zip(nullmap, nullmap_bytes):
        if isnull == None:
            retlines.append('[%s] %s <invalid byte>' % (col, byte))
        else:
            retlines.append('[%s] %s' % (col, isnull))
        col+=1

    retval = '\n'.join([line for line in retlines])

    if skip_tag:
        return add_indent('%s' % retval, indent, True)
    else:
        retval = '[%s]' % field + add_indent(retval, 1, True)
        return add_indent(retval, indent, True)

def get_tts_nullmap(node, nullmap_field, natts):
    if str(node[nullmap_field]) == '0x0':
        return None

    nullmap = []
    nullmap_byte = []
    for col in range(0, natts):
        if node[nullmap_field][col] not in [0,1]:
            nullmap.append(None)
        else:
            nullmap.append((int(node[nullmap_field][col]) == 1))

        nullmap_byte.append('0x%x' % node[nullmap_field][col])

    return nullmap, nullmap_byte

def format_tts_values(node, field, cast_to=None, skip_tag=False, print_null=False, indent=1):
    if str(node['tts_tupleDescriptor']) == '0x0' or str(node[field]) == '0x0':
        if print_null:
            return '[%s] (NULL)' % field
        else:
            return ''

    descr = node['tts_tupleDescriptor'].dereference()
    natts = descr['natts']

    nullmap, nullmap_bytes = get_tts_nullmap(node, 'PRIVATE_tts_isnull', natts)

    tts_values_list = []

    values = node[field]
    tts_values_list = []
    for col in range(0, natts):
        tts_values_list.append("[%d] 0x%08x" % (col + 1, values[col]))

    values_retval = '\n'.join([line for line in tts_values_list])

    formatted_tuple_list= []
    for col in range(0, natts):
        attr = tuple_desc_attr(descr, col).dereference()
        formatted_tuple_list.append("[%d] %s" % (col + 1, format_tuple_value(values[col], nullmap[col], attr)))

    formatted_tuples_retval  = '\n'.join([line for line in formatted_tuple_list])

    retval = '[%s]' % field + add_indent(values_retval, 1, True)
    retval += '\n[values_formatted_tuple]' + add_indent(formatted_tuples_retval, 1, True)
    return add_indent(retval, indent, True)

def format_tuple_value(value, is_null, attr):
    typid = attr['atttypid']

    if is_null:
        return 'NULL'
    elif typid == 20:
        return "[int8] %d" % value
    elif typid == 701:
        return "[float8] %s" % value.cast(gdb.lookup_type('float8'))
    else:
        return "<type %d not_supported>" % typid
#---

def debug_format_regular_field(node, field):
    node_type = get_base_node_type(node)
    if node_type == None:
        node_type = get_base_datatype_string(node)
    base_field_type = get_base_datatype_string(node[field])
    gdb_basic_type = gdb.types.get_basic_type(node[field].type)
    field_type = str(node[field].type)
    print("debug_format_regular_field: (field_type: %s gdb.types.get_basic_type: %s, base_field_type: %s) %s[%s]: %s" 
            % (field_type, gdb_basic_type, base_field_type, node_type, field, node[field]))
    return node[field]

def debug_format_string_pointer_field(node, field):
    print("debug_format_string_pointer_field : %s[%s]: " % (get_base_node_type(node), field), end='')
    ret = format_string_pointer_field(node,field)
    print(ret)
    return ret

def debug_format_char_field(node, field):
    print("debug_format_char_field: %s[%s]: " % (get_base_node_type(node), field), end='')
    ret = format_char_field(node,field)
    print(ret)
    return ret

def debug_format_bitmapset_field(node, field):
    print("debug_format_bitmapset_field: %s[%s]: " % (get_base_node_type(node), field), end='')
    ret = format_bitmapset_field(node,field)
    print(ret)
    return ret

def debug_format_varno_field(node, field):
    print("debug_format_varno_field: %s[%s]: " % (get_base_node_type(node), field), end='')
    ret = format_varno_field(node,field)
    print(ret)
    return ret

def debug_format_optional_node_field(node, fieldname, cast_to=None, skip_tag=False, print_null=False, indent=1):
    print("debug_format_optional_node_field: %s[%s]: cast_to=%s skip_tag=%s print_null=%s, indent=%s" %
            (get_base_node_type(node), fieldname, cast_to, skip_tag, print_null, indent), end='')
    ret = format_optional_node_field(node, fieldname, cast_to, skip_tag, True, indent)
    print(ret)
    return ret

def debug_format_optional_node_list(node, fieldname, cast_to=None, skip_tag=False, newLine=True, print_null=False, indent=1):
    print("debug_format_optional_node_list: %s[%s]: cast_to=%s skip_tag=%s print_null=%s, indent=%s" %
            (get_base_node_type(node), fieldname, cast_to, skip_tag, print_null, indent))
    ret = format_optional_node_list(node, fieldname, cast_to, skip_tag, newLine, True, indent)
    print(ret)
    return ret

def debug_format_optional_oid_list(node, fieldname, skip_tag=False, newLine=False, print_null=False, indent=1):
    print("debug_format_optional_oid_list: %s[%s]: cast_to=%s skip_tag=%s print_null=%s, indent=%s" %
            (get_base_node_type(node), fieldname, cast_to, skip_tag, print_null, indent))
    ret = format_optional_oid_list(node, fieldname, newLine, skip_tag, print_null, indent)
    print(ret)
    return ret

def debug_minimal_format_node_list(node, fieldname, cast_to=None, skip_tag=False, print_null=False, indent=1):
    print("debug_minimal_format_node_list: %s[%s]: cast_to=%s skip_tag=%s print_null=%s, indent=%s" % (get_base_node_type(node), fieldname,
        cast_to, skip_tag, print_null, indent))
    ret = minimal_format_node_list(node, fieldname, cast_to, skip_tag, True, indent)
    print(ret)
    return ret

def debug_format_pseudo_node_field(node, fieldname, cast_to=None, skip_tag=False, print_null=False, indent=1):
    print("debug_minimal_format_node_list: (%s) %s[%s]: cast_to=%s skip_tag=%s print_null=%s, indent=%s" % (get_base_datatype_string(node[fieldname]), get_base_datatype_string(node), fieldname,
        cast_to, skip_tag, print_null, indent))
    ret = format_pseudo_node_field(node, fieldname, cast_to, skip_tag, True, indent)
    print(ret)
    return ret


class NodeFormatter(object):
    # Basic node information
    _node = None
    _parent_node = None

    _type_string = None
    _base_type = None
    _node_type = None

    # String representations of individual fields in node
    _all_fields = None
    _regular_fields = None
    _node_fields = None
    _list_fields = None
    _tree_fields = None
    _ignore_field_types = None

    # Some node types are sub-types of other fields, for example a JoinState inherits a PlanState
    __nested_nodes = None

    _default_display_methods = None
    _default_regular_visibility = None
    _default_list_visibility = None
    _default_node_visibility = None
    _default_skip_tag = None
    _formatter_overrides = None

    # String representation of the types to match to generate the above lists
    _list_types = None
    _node_types = None
    def __init__(self, node, typecast=None, pseudo_node=False):

        # TODO: get node and list types from yaml config OR check each field
        #       for a node 'signature'
        # TODO: this should be done in a class method
        self._list_types = ["List"]
        self._node_types = ["Node"]

        self._pseudo_node = pseudo_node
        if typecast == None:
            if self._pseudo_node:
                pseudo_type = node.type.strip_typedefs()
                stripped_type = pseudo_type

                self._type_string = get_base_datatype_string(node)
                self._base_type = get_base_datatype(node)
                self._node_type = pseudo_type
                # TODO: Why does this work?
                #if self._node_type.code != gdb.TYPE_CODE_PTR:
                #    raise Exception("Must use a pointer for pseudo node types")
            else:
                self._type_string = get_base_node_type(node)
                if self._type_string == None:
                    raise gdb.error("%s does not look like a Node" % node)
                self._base_type = gdb.lookup_type(self._type_string)
                self._node_type = self._base_type.pointer()
        else:
            self._type_string = typecast
            self._base_type = gdb.lookup_type(self.type_string)
            self._node_type = self._base_type.pointer()

        if self._node_type.strip_typedefs().code == gdb.TYPE_CODE_PTR:
            node = as_pointer(node)

        self._node = node.cast(self._node_type)


        # Get methods for display
        self._default_display_methods = DEFAULT_DISPLAY_METHODS
        self._default_regular_visibility = ALWAYS_SHOW
        self._default_list_visibility = NOT_NULL
        self._default_node_visibility = NOT_NULL
        self._default_skip_tag = False
        self._formatter_overrides = FORMATTER_OVERRIDES.get(self.type_string)
        #print("NodeFormatter:", self.type)

    def is_child_node(self):
        if self._pseudo_node:
            return False

        t = gdb.lookup_type(self.type_string)
        first_field = t.values()[0]

        if first_field.name == "type":
            return False
        elif first_field.name == "xpr":
            return False
        elif first_field.name == "xprstate":
            return False

        return True

    @property
    def parent_node(self):
        if self._parent_node == None:
            if self.is_child_node():
                t = gdb.lookup_type(self.type_string)
                first_field = t.values()[0]

                self._parent_node = NodeFormatter(self._node, str(first_field.type))
        return self._parent_node


    def get_datatype_override(self, field):
        if self._formatter_overrides != None:
            datatype_overrides = self._formatter_overrides.get('datatype_methods')
            if datatype_overrides != None:
                return datatype_overrides.get(str(self.field_datatype(field)))
        return None


    def get_field_override(self, field, override_type):
        if self._formatter_overrides != None:
            field_overrides = self._formatter_overrides.get('fields')
            if field_overrides != None:
                field_override = field_overrides.get(field)
                if field_override != None:
                    return field_override.get(override_type)
        return None

    def get_display_method(self, field):
        # Individual field overrides are a higher priority than type
        # overrides so print them first
        field_override_method_name = self.get_field_override(field, 'formatter')
        if field_override_method_name != None:
            return globals()[field_override_method_name]

        # Datatype methods are only for regular fields
        datatype_override_method = self.get_datatype_override(field)
        if datatype_override_method != None:
            return globals()[datatype_override_method]

        # Check if this datatype has a generic dumping method
        default_type_method = self._default_display_methods['datatype_methods'].get(str(self.field_datatype(field)))
        if default_type_method != None:
            return globals()[default_type_method]

        if field in self.regular_fields:
            return globals()[self._default_display_methods['regular_fields']]
        elif field in self.node_fields:
            return globals()[self._default_display_methods['node_fields']]
        elif field in self.list_fields:
            return globals()[self._default_display_methods['list_fields']]
        elif field in self.tree_fields:
            return globals()[self._default_display_methods['tree_fields']]

        raise Exception("Did not find a display method for %s[%s]" % (self.type_string, field))


    def get_display_mode(self, field):
        # If the global 'show_hidden' is set, then this command shal always
        # return ALWAYS_SHOW
        if self._default_display_methods['show_hidden'] == True:
            return ALWAYS_SHOW

        override_string = self.get_field_override(field, 'visibility')
        if override_string != None:
            return override_string

        if field in self.regular_fields:
            return self._default_regular_visibility
        if field in self.list_fields:
            return self._default_list_visibility
        if field in self.node_fields:
            return self._default_node_visibility

        return ALWAYS_SHOW

    def is_skip_tag(self, field):
        # If the global 'show_hidden' is set, always show tag
        if self._default_display_methods['show_hidden'] == True:
            return False

        skip_tag = self.get_field_override(field, 'skip_tag')
        if skip_tag != None:
            return skip_tag

        return self._default_skip_tag

    @property
    def type_string(self):
        return self._type_string

    @property
    def fields(self):
        if self._all_fields == None:
            self._all_fields = []
            t = self._base_type

            for index in range(0, len(t.values())):
                skip = False

                field = t.values()[index]
                # TODO: should the ability to ignore fields entirely exist at all?
                #       This seems to conflict with the visibility settings
                # Fields that are to be ignored
                if self._ignore_field_types is not None:
                    for tag in self._ignore_field_types:
                        if self.is_type(field, tag):
                            skip = True

                if index == 0:
                    # The node['type'] field is just a tag that we already know
                    if field.name == "type":
                        skip = True
                    # The node['xpr'] field is just a wrapper around node['type']
                    elif field.name == "xpr":
                        skip = True
                    elif field.name == "xprstate":
                        skip = True
                    elif self.is_child_node():
                        skip = True
                if skip:
                    continue

                self._all_fields.append(field.name)

        return self._all_fields

    @property
    def list_fields(self):
        if self._list_fields == None:
            self._list_fields = []

            for f in self.fields:
                # Honor overrides before all else
                override_string = self.get_field_override(f, 'field_type')
                if override_string != None:
                    if override_string == 'list_field':
                        self._list_fields.append(f)
                    else:
                        continue

                v = self._node[f]
                for field in self._list_types:
                    if self.is_type(v, field):
                        self._list_fields.append(f)

        return self._list_fields

    @property
    def node_fields(self):
        if self._node_fields == None:
            self._node_fields = []

            for f in self.fields:
                override_string = self.get_field_override(f, 'field_type')
                if override_string != None:
                    if override_string == 'node_field':
                        self._node_fields.append(f)
                    else:
                        continue

                v = self._node[f]
                for field in self._node_types:
                    if self.is_type(v, field):
                        self._node_fields.append(f)

                if is_node(v):
                    if f not in self.list_fields:
                        self._node_fields.append(f)

        return self._node_fields

    @property
    def tree_fields(self):
        if self._tree_fields == None:
            self._tree_fields = []
            for f in self.fields:
                override_string = self.get_field_override(f, 'field_type')
                if override_string != None:
                    if override_string == 'tree_field':
                        self._tree_fields.append(f)
                    else:
                        continue

        return self._tree_fields

    @property
    def regular_fields(self):
        if self._regular_fields == None:
            self._regular_fields = []

            self._regular_fields = [field for field in self.fields if field not in self.list_fields + self.node_fields + self.tree_fields]

        return self._regular_fields

    def is_type(self, value, type_name):
        try:
            t = gdb.lookup_type(type_name)
        except gdb.error:
            # no such type in this binary (e.g. no 'List' outside postgres)
            return False
        return (get_base_datatype(value) == get_base_datatype(t))

    def field_datatype(self, field):
        return gdb.types.get_basic_type(self._node[field].type)

    def format(self, prefix=None):
        retval = ''
        if prefix != None:
            retval = prefix
        retval += self.type_string + ' '
        newline_padding_chars = len(retval)
        formatted_fields = self.format_all_regular_fields(newline_padding_chars+1)

        fieldno = 1
        for field in formatted_fields:
            retval += field
            if fieldno < len(formatted_fields):
                retval += '\n' + ' ' * newline_padding_chars
            fieldno += 1

        retval += self.format_complex_fields()

        retval += self.format_tree_nodes()

        return retval

    def format_regular_fields(self, newline_padding_chars):
        # TODO: get this value from config file
        max_regular_field_chars = 140
        retval = "["

        fieldcount = 0
        retline = ""
        for field in self.regular_fields:
            fieldcount +=1
            display_mode = self.get_display_mode(field)
            if display_mode == NEVER_SHOW:
                continue

            # Some fields don't have a meaning if they aren't given a value
            if display_mode == NOT_NULL:
                field_datatype = self.field_datatype(field)
                empty_value = gdb.Value(0).cast(field_datatype)
                if self._node[field] == empty_value:
                    continue

            # Some fields are initialized to -1 if they are not used
            if display_mode == HIDE_INVALID:
                field_datatype = self.field_datatype(field)
                empty_value = gdb.Value(-1).cast(field_datatype)
                if self._node[field] == empty_value:
                    continue

            value = self.format_regular_field(field)


            # TODO: display method did not return a value- fallback to the
            # default display method
            #if value == None:
            #    continue

            if fieldcount > 1:
                # TODO: track current indentation level
                if len(retline) > max_regular_field_chars:
                    retval += retline + '\n' + (' ' * newline_padding_chars)
                    retline = ''
                elif len(retline) > 0:
                    retline += ' '

            retline += "%(field)s=%(value)s" % {
                'field': field,
                'value': value
            }

        retval += retline
        retval += ']'

        return retval

    def format_regular_field(self, field):
        global recursion_depth

        display_method = self.get_display_method(field)
        depth = recursion_depth
        try:
            return display_method(self._node, field)
        except Exception as e:
            # One unreadable field must not cost us the whole dump.  A field
            # that threw halfway through format_node() left recursion_depth
            # raised, so put it back.
            recursion_depth = depth
            return "<%s: %s>" % (type(e).__name__, e)

    def format_complex_fields(self):
        retval = ""
        if self.is_child_node():
            retval += self.parent_node.format_complex_fields()

        for field in self.fields:
            if field in self.regular_fields:
                continue
            if field in self.tree_fields:
                continue
            retval += self.format_complex_field(field)

        return retval

    def format_complex_field(self, field):
        global recursion_depth

        display_mode = self.get_display_mode(field)
        print_null = False
        if display_mode == NEVER_SHOW:
            return ""
        elif display_mode == ALWAYS_SHOW:
            print_null = True

        skip_tag = self.is_skip_tag(field)

        display_method = self.get_display_method(field)
        depth = recursion_depth
        try:
            return display_method(self._node, field, skip_tag=skip_tag, print_null=print_null)
        except Exception as e:
            recursion_depth = depth
            return add_indent("[%s] <%s: %s>" % (field, type(e).__name__, e), 1, True)

    def format_all_regular_fields(self, offset):
        formatted_fields = []
        if self.parent_node != None:
            formatted_fields += self.parent_node.format_all_regular_fields(offset)

        formatted_fields.append(self.format_regular_fields(offset))
        return formatted_fields


    def ignore_type(self, field):
        if self._ignore_field_types is None:
            self._ignore_field_types = []
        self._ignore_field_types.append(field)

        # reset list of all fields
        self._list_fields = None
        self._regular_fields = None
        self._all_fields = None

    def format_tree_nodes(self):
        retval = ""
        for field in self.tree_fields:
            retval += self.format_complex_field(field)

        if retval == "" and self.is_child_node():
            retval += self.parent_node.format_tree_nodes()

        return retval

class LabelNodeFormatter(NodeFormatter):
    def __init__(self, node, typecast=None, pseudo_node=False, label=''):
        self._label = label
        super().__init__(node, typecast, pseudo_node)
    def format(self):
        return super().format(prefix=self._label)

class PlanStateFormatter(NodeFormatter):
    def format(self):
        return super().format(prefix='-> ')

class PgPrintCommand(gdb.Command):
    "print PostgreSQL structures"

    def __init__(self):
        super(PgPrintCommand, self).__init__("pgprint", gdb.COMMAND_SUPPORT,
                                             gdb.COMPLETE_NONE, False)

    def invoke(self, arg, from_tty):
        global recursion_depth

        arg_list = gdb.string_to_argv(arg)
        if len(arg_list) not in (1, 2):
            print("usage: pgprint var [natts]")
            return

        # Optional row width for arrays whose element count is not stored
        # next to them (range-partition boundinfo->datums rows).
        natts = None
        if len(arg_list) == 2:
            try:
                natts = int(arg_list[1])
            except ValueError:
                print("usage: pgprint var [natts]")
                return
        recursion_depth = 0

        l = gdb.parse_and_eval(arg_list[0])

        # Peel off extra pointer levels (e.g. 'pgprint args_p' where
        # args_p is a 'List **' function argument) so the user doesn't
        # have to type '*args_p' themselves.
        while True:
            t = l.type.strip_typedefs()
            if (t.code != gdb.TYPE_CODE_PTR or
                    t.target().strip_typedefs().code != gdb.TYPE_CODE_PTR):
                break
            if int(l) == 0:
                print("(NULL)")
                return
            l = l.dereference()

        if not is_node(l):
            # Members of a PartitionBoundInfoData (boundinfo->datums, ...)
            # can only be sized with the ndatums/strategy sitting next to
            # them, so route them through the boundinfo-aware dumper.
            if print_partition_bound_member(arg_list[0], natts):
                return

            # The pseudo-node dump walks struct members; scalars, enums and
            # pointers to scalars (e.g. a bare 'Datum *') have none, and
            # NodeFormatter would die with "Type is not a structure, union,
            # enum, or function type."
            try:
                # get_base_datatype() peels pointers but can leave a typedef
                # (e.g. 'PartitionBoundInfoData') in place, so strip it
                # before looking at the type code
                base_code = get_base_datatype(l).strip_typedefs().code
            except Exception:
                base_code = None
            if base_code not in (gdb.TYPE_CODE_STRUCT, gdb.TYPE_CODE_UNION):
                print("(%s) %s" % (l.type, l))
                return

            print("not a node type")
            print("running experimental dump...")
            formatter = NodeFormatter(l, pseudo_node=True)
            print(formatter.format())
        else:
            print(format_node(l))

PgPrintCommand()
