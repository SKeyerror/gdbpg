Postgres and GPDB GDB commands
=======================

GDB commands making debugging Postgres and Greenplum internals easier.
Currently this provides a single command 'pgprint' that understand the 'Node'
structures used internally, can can print them semi-intelligently.

Forked from PostgreSQL debugging tool found here:

https://github.com/tvondra/gdbpg


Usage
-----

Copy the `gdbpg.py` script somewhere, and load it from `gdb`:

    $ gdb ... 
    (gdb) source /...path.../gdbpg.py 

and then just use `pgprint` command to print variables from gdb console.
E.g. if `plan` is pointing to `(PlannedStmt *)`, you may do this:
    
    (gdb) pgprint plan

and you'll get something like this:

```
PlannedStmt [commandType=CMD_SELECT planGen=PLANGEN_PLANNER queryId=0 hasReturning=false hasModifyingCTE=false canSetTag=true transientPlan=false oneoffPlan=false
             simplyUpdatable=false dependsOnRole=false parallelModeNeeded=false numSlices=2 slices=0x56059fb60a08 subplan_sliceIds=0x56059fb61458 rewindPlanIDs=0x0
             nParamExec=0 query_mem=0 metricsQueryType=0 '\000']
	[planTree] 
		-> Motion [startup_cost=0 total_cost=2989 plan_rows=96300 plan_width=4 parallel_aware=false plan_node_id=0]
		          [motionType=MOTIONTYPE_GATHER sendSorted=false motionID=1 collations=0x0]
			[targetlist] 
				TargetEntry [resno=1 resname="a" resorigtbl=16392 resorigcol=1]
					Var [varno=OUTER_VAR varattno=1 vartype=23 varnoold=1 varoattno=1]
			[flow] Flow [flotype=FLOW_SINGLETON locustype=CdbLocusType_Entry segindex=-1 numsegments=3]
			[lefttree] 
				-> SeqScan [startup_cost=0 total_cost=1063 plan_rows=96300 plan_width=4 parallel_aware=false plan_node_id=1]
				           [scanrelid=1]
					[targetlist] 
						TargetEntry [resno=1 resname="a" resorigtbl=16392 resorigcol=1]
							Var [varno=1 varattno=1 vartype=23 varnoold=1 varoattno=1]
					[flow] Flow [flotype=FLOW_PARTITIONED locustype=CdbLocusType_Hashed segindex=0 numsegments=3]
	[rtable] 
		RangeTblEntry [rtekind=RTE_RELATION relid=16392 relkind='r' jointype=JOIN_INNER funcordinality=false ctelevelsup=0 self_reference=false forceDistRandom=false
		               lateral=false inFromCl=true requiredPerms=2 selectedCols=0x00000400]
	[relationOids] OidList: [16392]
```


Partitioned tables
------------------

`pgprint` understands the (non-Node) partitioning structs from
`partdesc.c` / `partbounds.c` / `partcache.c`:

* `PartitionBoundInfoData` -- `datums` / `kind` / `indexes` sized from
  `ndatums` / `nindexes`, with the bound Datums decoded by the key column
  types (date, timestamp, numeric, text, uuid, int*, ...); `MINVALUE` /
  `MAXVALUE` are spelled out, hash rows show `(modulus, remainder)`.
* `PartitionKeyData` / `PartitionSchemeData` -- one `[columns]` line per key
  column gathering attnum, type, typlen, collation, opfamily and support
  function.
* `PartitionDescData` -- `oids` / `is_leaf` plus a `[partitions]` summary that
  pairs every partition with its bounds, e.g.
  `[1] oid=57474 leaf=true FOR VALUES FROM (2020-01-01) TO (2021-01-01)`.
* `PartitionDirectoryData` -- walks the dynahash table and dumps every
  `PartitionDirectoryEntry` with its `PartitionDesc`.
* `PartitionBoundSpec` (a Node) -- its `Const` bounds are decoded by
  `consttype`.

The row width and column types of range bounds are not stored in the
boundinfo; they come from the partition key.  They are picked up automatically
when the key sits next to the partdesc in what you print (`Relation`,
`PartitionDispatch`, `RelOptInfo`, `PartitionDirectory`), otherwise pass it as
a second argument:

    (gdb) pgprint pd                             # PartitionDispatch: key + partdesc
    (gdb) pgprint rel->rd_partdesc rel           # a Relation, or rel->rd_partkey
    (gdb) pgprint rel->boundinfo rel             # RelOptInfo: uses part_scheme
    (gdb) pgprint boundinfo->datums key          # any PartitionKey expression
    (gdb) pgprint boundinfo 2                    # just the row width, raw Datums

```
PartitionDescData [nparts=5 detached_exist=false last_found_datum_index=-1 last_found_count=0]
    [oids] (nparts=5) [57469, 57474, 57479, 57484, 57489]
    [is_leaf] (nparts=5) [true, true, true, true, true]
    [boundinfo]
        PartitionBoundInfoData [strategy='r' ndatums=6 interleaved_parts=0x0 nindexes=7 null_index=-1 default_index=4]
            [datums] (strategy='r' ndatums=6 natts=1 types=[date] from PartitionKeyData 0x6122ffd99900)
                [0] (MINVALUE)
                [1] (2020-01-01)
                [2] (2021-01-01)
                [3] (2022-01-01)
                [4] (2023-01-01)
                [5] (MAXVALUE)
            [kind] (ndatums=6)
                [0] (PARTITION_RANGE_DATUM_MINVALUE)
                ...
            [indexes] (nindexes=7) [-1, 0, 1, 2, -1, 3, -1]
    [partitions]
        [0] oid=57469 leaf=true FOR VALUES FROM (MINVALUE) TO (2020-01-01)
        [1] oid=57474 leaf=true FOR VALUES FROM (2020-01-01) TO (2021-01-01)
        [2] oid=57479 leaf=true FOR VALUES FROM (2021-01-01) TO (2022-01-01)
        [3] oid=57484 leaf=true FOR VALUES FROM (2023-01-01) TO (MAXVALUE)
        [4] oid=57489 leaf=true DEFAULT
```


ORCA: `print` that does not hang
--------------------------------

`orcaprint.py` replaces gdb's `print` / `p` / `inspect`. In ORCA (C++)
frames, gdb 15 can hang forever, at 100% CPU and ignoring Ctrl-C, on things as
simple as `p pexprInner->Arity()`. For `x->Name` the C++ parser looks up
`Name` through every `using namespace` directive (`cp_lookup_symbol_via_imports`).
ORCA translation units have hundreds of those (CUtils.cpp has 402), and the
search is exponential.

In C++ frames, expressions made only of variables, `*`, `&`, parentheses, `->`,
`.`, `[N]` and method calls with integer arguments are evaluated through
gdb's Python API instead:

    (gdb) p pexprInner->Arity()
    (gdb) p (*(*pexpr)[1])[0]->Pop()->SzId()
    (gdb) p/d pexpr->Pop()->Eopid()          # virtual call dispatches on the dynamic type

Methods are found by looking up `ns::Class::Method` as a global symbol, so
no name goes through the using-directive search. `operator[]` is called for
`[N]` on objects. Anything else (casts, arithmetic, `$` variables, print
options) goes to gdb's own evaluator unchanged. Turn it off with
`set orca-safe-print off`.


ORCA: `CExpression` trees
-------------------------

`orcaprint <CExpression*>` prints the tree in the same format as ORCA's
`CExpression::OsPrint`, i.e. what `optimizer_print_query` /
`optimizer_print_plan` log, so the output can be diffed against the log.
Everything is read from memory; no inferior function is called. The one
intended difference is text constants: ORCA prints their stats hash
(`CScalarConst (820420200.000)`), orcaprint prints the string (`CScalarConst ('ab%')`).

`orcaprint <CQueryContext*>` (e.g. `orcaprint pqc`) prints what
`CQueryContext::OsPrint` / `pqc->DbgPrint()` prints: the `m_pexpr` tree, a blank
line, then the required plan properties
(`req CTEs: [..], req order: [..], req dist: [..], ...`). The options below
also work on a `CQueryContext` and apply to its `m_pexpr`.

Options (default output is unchanged):

    orcaprint -n pexpr           # append each node's path: @root, @0, @0.1, ...
    orcaprint pexpr @0.1         # print only that subtree (child 0 of root, then its child 1)
    orcaprint -p pexpr           # derived properties that ORCA has already computed
    orcaprint --check pexpr      # mark nodes that reference columns nobody produces
    orcaprint colref v pexpr     # where a column (by name or id) is produced, used, outer ref

`-p` only shows what is already derived (`m_is_prop_derived`); it never
triggers derivation. Logical/physical nodes get
`[drvd] out=[..] outer=[..] keys={..} maxcard=N`. Physical nodes also get
`[plan]` (derived dist/order/rewind) and `[reqd]` (required cols/dist/order/rewind),
which shows why an enforcer (Motion, Sort, Spool) was added. Scalar nodes
get `used=` / `defined=`.

    +--CLogicalProject
       |  [drvd] out=?  outer=["v" (1)]
       |--CLogicalSelect
       |  |  [drvd] out=["id" (10), "p" (11), ...]  outer=["v" (1)]

`--check` flags (`!!`) a column reference with no producer anywhere in the
tree (a dangling colref, or an outer ref when printing a subtree). When
properties are derived, it also flags a node whose scalar children use
columns that are neither output by its relational children nor in its outer refs.

`colref` lists every node that produces (`def`) or references (`use`) a
column, with its path and operator field, plus nodes where it is an outer ref:

    column "v" (1):
      def   @0.0            CLogicalGet.m_pdrgpcrOutput
      use   @0.1.0.0        CScalarIdent.m_pcr
      outer @0.1.1.0        CLogicalProject  (outer ref of this subtree)
      use   @0.1.1.0.0.1.1  CScalarIdent.m_pcr

### `orcatrace`: which preprocessing step changed the tree

    (gdb) orcatrace                # default: CExpressionPreprocessor::PexprPreprocess
    (gdb) orcatrace --full         # print each changed tree in full instead of a diff
    (gdb) orcatrace <function>     # any function that chains CExpression* locals

It steps (`next`) through the function. Each time a `CExpression*` local is
assigned, it diffs the new tree against the previous one and names the step
from the source line:

    -- [8] PexprReplaceColWithConst -> pexprPredWithConstReplaced: +8 -4  (line 3731)
    @@ -1,8 +1,12 @@
    ...
    == 31 step(s), 3 changed the tree:
       [8] PexprReplaceColWithConst -> pexprPredWithConstReplaced  +8 -4
       [9] PexprReorderScalarCmpChildren -> pexprReorderedScalarCmpChildren  +2 -2
       [14] PexprInferPredicates -> pexprInferredPreds  +27 -27

If you are not in that function yet, it sets a temporary breakpoint and
continues. Other breakpoints are disabled during the trace and re-enabled
afterwards. When it finishes, you are stopped in the caller.
