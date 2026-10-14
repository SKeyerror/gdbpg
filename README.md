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
