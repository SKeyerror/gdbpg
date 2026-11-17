# -*- coding: UTF-8 -*-

import gdb, io, sys
import re

class SilenceGDBWarnings:
    """Temporarily redirect GDB internal warnings to /dev/null,
    while keeping Python print() output visible.

    print() 的输出先缓存在内存里，等关闭日志重定向后再统一经 gdb.write
    输出。不能直接写 sys.__stdout__：那会绕过 gdb 的终端处理，缓冲区在
    (gdb) 提示符之后才刷出，此时终端处于 readline 模式，\\n 不回到行首，
    输出变成阶梯状并和提示符交错。"""
    def __enter__(self):
        # 先保存原 stdout/stderr
        self.old_stdout = sys.stdout
        self.old_stderr = sys.stderr

        gdb.execute("set width 0", to_string=True)   # 不限制行宽，避免自动换行

        # 打开 GDB 的日志重定向
        gdb.execute("set logging file /dev/null")
        gdb.execute("set logging redirect on")
        gdb.execute("set logging overwrite on")
        gdb.execute("set logging enabled on")

        # Python 的 print() 先写入缓冲区
        self.buf = io.StringIO()
        sys.stdout = self.buf
        sys.stderr = self.buf
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # 恢复 Python 输出
        sys.stdout = self.old_stdout
        sys.stderr = self.old_stderr

        # 关闭 GDB 日志重定向
        gdb.execute("set logging enabled off")
        gdb.execute("set logging redirect off")

        # 经 gdb 的输出流写到终端，即使中途抛异常也把已缓存部分输出
        gdb.write(self.buf.getvalue())
        gdb.flush()

MAX_INDENT = 12  # 最多缩进 12 层（自己可调）
MAX_ARRAY_ELEMS = 100000  # 数组长度上限，超过视为脏内存
MAX_DEPTH = 200           # 递归深度上限，防止环/野指针无限递归

# 已释放/未初始化内存的典型填充值 (PG CLOBBER_FREED_MEMORY = 0x7F, GPOS/glibc 常见填充)
_CLOBBER_PATTERNS = {0x7F7F7F7F, 0x7F7F7F7F7F7F7F7F, 0xCDCDCDCD, 0xCDCDCDCDCDCDCDCD,
                     0xDEADBEEF, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}

def array_size(arr):
    """读取 CDynamicPtrArray 的 m_size 并做合法性检查。

    指向已释放内存时 m_size 会是 0x7F7F7F7F 之类的垃圾值，直接
    range(m_size) 会让 gdb 跑上几天且 Ctrl-C 无效，所以这里提前拒绝。"""
    size = int(arr['m_size'])
    cap = None
    try:
        cap = int(arr['m_capacity'])
    except Exception:
        pass
    if (size < 0 or size > MAX_ARRAY_ELEMS or (size & 0xFFFFFFFFFFFFFFFF) in _CLOBBER_PATTERNS
            or (cap is not None and size > cap)):
        raise gdb.GdbError(f"bogus array m_size={size:#x} capacity={cap if cap is None else hex(cap)} "
                           f"@ {arr.address} (freed or uninitialized memory?)")
    return size

def mkpad(depth):
    d = depth
    if d < 0:
        d = 0
    if d > MAX_INDENT:
        d = MAX_INDENT
    return " " * (d * 2)

def safe_deref(ptr):
    try:
        if ptr == 0:
            return None
        return ptr.dereference()
    except Exception:
        return None

def get_str(val):
    try:
        if val is None:
            return "NULL"
        if str(val.type).endswith('*'):
            return f"{val}"
        return str(int(val))
    except Exception:
        return f"{val}"

def parse_cmdname(val):
    return get_cw_string(val['m_name'])

def get_cw_string(val):
    m = re.search(r'L?"([^"]+)"', str(val['m_w_str_buffer']))
    if m:
        return m.group(1)
    return ""

def get_mdname(val):
    mdname = parse_cmdname(val['m_mdname'])
    return mdname

def get_oid_from_mdid(mdid_ptr):
    """从 IMDId* 中提取 OID (假设是 CMDIdGPDB)"""
    if not mdid_ptr or int(mdid_ptr) == 0:
        return "NULL"
    try:
        # 尝试转换为 CMDIdGPDB 并读取 m_oid
        gpdb_mdid = mdid_ptr.cast(gdb.lookup_type("gpmd::CMDIdGPDB").pointer())
        return int(gpdb_mdid['m_oid'])
    except Exception as e:
        print(e)
        return "unknown"

def get_dynamic_ptr_array_list(array_ptr, item_type="ULONG"):
    """
    解析 CDynamicPtrArray 并返回 Python 列表
    """
    if not array_ptr or int(array_ptr) == 0:
        return []

    res = []
    array_obj = array_ptr.dereference()
    size = array_size(array_obj)
    elems = array_obj['m_elems']

    for i in range(size):
        item_ptr = (elems + i).dereference()
        if item_ptr != 0:
            # 如果是基础类型如 ULONG，直接转 int
            if item_type == "ULONG":
                res.append(int(item_ptr.dereference()))
            else:
                res.append(item_ptr)
    return res

def print_cdxltabledescr(val):
    print("=" * 80)
    print(f"CDXLTableDescr @ {val.address}")
    mdid = val['m_mdid']
    mdname = parse_cmdname(val['m_mdname'])
    user_id = int(val['m_execute_as_user_id'])
    lockmode = int(val['m_lockmode'])
    acl = int(val['m_acl_mode'])
    qid = int(val['m_assigned_query_id_for_target_rel'])
    print(f"  mdid:               {get_str(mdid)}")
    print(f"  mdname:             {get_str(mdname)}")
    print(f"  execute_as_user_id: {user_id}")
    print(f"  lockmode:           {lockmode}")
    print(f"  acl_mode:           {acl}")
    print(f"  assigned_query_id:  {qid}")

    # columns (optional best-effort)
    if 'm_dxl_column_descr_array' in val.type.keys():
        cols = val['m_dxl_column_descr_array']
        print("\n  Columns:")
        if cols == 0:
            print("    (no columns)")
        else:
            try:
                num = array_size(cols.dereference())
                arr = cols['m_elems']
                for i in range(num):
                    col = safe_deref((arr + i).dereference())
                    if not col:
                        continue
                    cid = int(col['m_column_id'])
                    attr = int(col['m_attr_no'])
                    is_drop = bool(col['m_is_dropped'])
                    width = int(col['m_column_width'])
                    cname = parse_cmdname(col['m_md_name'])
                    print(f"    [{i}] id={cid}, attr={attr}, width={width}, "
                          f"is_dropped={'true' if is_drop else 'false'}, name={get_str(cname)}")
            except Exception as e:
                print(f"    [warn] failed to parse column array: {e}")
    print("=" * 80)


def get_dxl_datum_str(pdatum):
    """从 CDXLDatum* 中提取实际值字符串 (CDXLScalarConstValue::m_dxl_datum)"""
    try:
        if int(pdatum) == 0:
            return "NULL"
        dyn = str(pdatum.dereference().dynamic_type).split("::")[-1].rstrip().rstrip('*').rstrip()
        # (类型名, 值字段) —— 字段名随 GPDB 版本可能不同，依次尝试
        known = {
            "CDXLDatumInt2": ("m_val", "m_value"),
            "CDXLDatumInt4": ("m_val", "m_value"),
            "CDXLDatumInt8": ("m_val", "m_value"),
            "CDXLDatumBool": ("m_value", "m_val"),
            "CDXLDatumOid": ("m_oid_val", "m_val"),
        }
        cast_ptr = pdatum.cast(gdb.lookup_type(f"gpdxl::{dyn}").pointer())
        is_null = False
        try:
            is_null = bool(cast_ptr['m_is_null'])
        except Exception:
            pass
        if is_null:
            return f"{dyn.replace('CDXLDatum', '')}: NULL"
        short = dyn.replace("CDXLDatum", "")
        for tname, fields in known.items():
            if dyn.startswith(tname):
                for f in fields:
                    if _has_field(cast_ptr.dereference(), f):
                        v = cast_ptr[f]
                        s = ("true" if bool(v) else "false") if tname == "CDXLDatumBool" else str(int(v))
                        return f"{short}: {s}"
        # Generic / Double / 其它：打印 byte 数组 + 长度
        try:
            length = int(cast_ptr['m_length'])
            data = cast_ptr['m_byte_array']
            if length > 0 and length <= 64 and int(data) != 0:
                raw = bytes(int((data + i).dereference()) & 0xFF for i in range(length))
                return f"{short}: len={length} bytes=0x{raw.hex()}"
            return f"{short}: len={length}"
        except Exception:
            pass
        try:
            return f"{short}: {float(cast_ptr['m_val'])}"
        except Exception:
            return short
    except Exception as e:
        return f"<error: {e}>"


def print_cdxlnode(val, depth=0, suffix=""):
    pad = mkpad(depth)
    if depth > MAX_DEPTH:
        print(pad + f"  [warn] depth > {MAX_DEPTH}, stop (cycle or garbage?)")
        return
    # print(pad + f"CDXLNode @ {val.address}")
    m_op = val['m_dxl_op']
    m_children = val['m_dxl_array']

    # 动态类型判断
    dyn_type = str(m_op.dynamic_type).split("::")[-1]
    dyn_type = dyn_type.rstrip().rstrip('*').rstrip()
    # 动态类型解析成抽象基类，说明 vtable 已失效，对象多半已被释放
    if dyn_type == "CDXLOperator":
        print(pad + f"  [warn] CDXLNode @ {val.address}: m_dxl_op has no valid vtable, "
              "node is likely freed/uninitialized -- not expanding")
        return

    # 增加 suffix 用于显示 (args), (aggorder) 等
    header = pad + f"  ▶ {dyn_type}"
    if suffix:
        header += f" ({suffix})"

    if "CDXLLogicalGet" in dyn_type:
        m_op_dynamic = m_op.cast(gdb.lookup_type("gpdxl::CDXLLogicalGet").pointer())
        print(header + f"  {get_mdname(m_op_dynamic['m_dxl_table_descr'])}")

    elif "CDXLScalarComp" in dyn_type:
        m_op_dynamic = m_op.cast(gdb.lookup_type("gpdxl::CDXLScalarComp").pointer())
        print(header + f"  {get_cw_string(m_op_dynamic['m_comparison_operator_name'])}")

    elif "CDXLScalarIdent" in dyn_type:
        m_op_dynamic = m_op.cast(gdb.lookup_type("gpdxl::CDXLScalarIdent").pointer())
        print(header + f"  {get_mdname(m_op_dynamic['m_dxl_colref'])} colid: {m_op_dynamic['m_dxl_colref']['m_id']}")

    elif "CDXLScalarConstValue" in dyn_type:
        m_op_dynamic = m_op.cast(gdb.lookup_type("gpdxl::CDXLScalarConstValue").pointer())
        print(header + f"  {get_dxl_datum_str(m_op_dynamic['m_dxl_datum'])}")

    elif "CDXLScalarProjElem" in dyn_type:
        m_op_dynamic = m_op.cast(gdb.lookup_type("gpdxl::CDXLScalarProjElem").pointer())
        print(header + f"  {get_mdname(m_op_dynamic)} colid: {m_op_dynamic['m_id']}")

    elif "CDXLLogicalGroupBy" in dyn_type:
        m_op_dynamic = m_op.cast(gdb.lookup_type("gpdxl::CDXLLogicalGroupBy").pointer())
        # 使用上个回答中提到的 get_dynamic_ptr_array_list 辅助函数
        ids = get_dynamic_ptr_array_list(m_op_dynamic['m_grouping_colid_array'])
        print(header + f"  Grouping Cols: {ids}")

    elif "CDXLScalarAggref" in dyn_type:
        m_op_dynamic = m_op.cast(gdb.lookup_type("gpdxl::CDXLScalarAggref").pointer())

        # 提取核心属性
        func_oid = get_oid_from_mdid(m_op_dynamic['m_agg_func_mdid'])
        is_distinct = "DISTINCT " if bool(m_op_dynamic['m_is_distinct']) else ""

        # 提取参数类型列表 (ULongPtrArray)
        arg_types = get_dynamic_ptr_array_list(m_op_dynamic['m_argtypes'])

        print(header + f"  {is_distinct}FuncOID: {func_oid}, ArgTypes: {arg_types}")

    elif "CDXLScalarValuesList" in dyn_type:
        # 打印出该列表包含多少个元素
        num_items = 0
        if m_children != 0:
            num_items = array_size(m_children.dereference())
        print(header + f"  [items: {num_items}]")

    else:
        print(header)

    if m_children != 0:
        try:
            num = array_size(m_children.dereference())
            arr = m_children['m_elems']

            # 定义 Aggref 子节点的标签映射
            agg_labels = ["args", "aggdirectargs", "aggorder", "aggdistinct", "aggfilter"]

            if num > 0:
                # print(pad + f"  children[{num}]:")
                for i in range(num):
                    child_ptr = (arr + i).dereference()
                    child = safe_deref(child_ptr)
                    if child:
                        # 如果是 Aggref，根据索引传递标签
                        child_suffix = ""
                        if "CDXLScalarAggref" in dyn_type and i < len(agg_labels):
                            child_suffix = agg_labels[i]
                        print_cdxlnode(child, depth + 2, child_suffix)
        except Exception as e:
            print(pad + f"  [warn] cannot expand children: {e}")
    else:
        pass


# --------- helpers ----------
def _has_field(val, name):
    try:
        _ = val[name]
        return True
    except Exception:
        return False

def _try_double(val):
    try:
        # 常见 CCost 内部字段名 m_dVal / m_dCost
        for k in ('m_dVal', 'm_dCost', 'm_d'):
            if _has_field(val, k):
                return float(val[k])
        # 退化为字符串
        return float(str(val))
    except Exception:
        return None

def shorten_type_name(t):
    s = str(t)
    # 取最后一个 '::' 之后的名字，并清理模板噪音
    if '::' in s:
        s = s.split('::')[-1]
    s = s.replace('gpopt::', '').replace('gpdxl::', '').replace('gpos::','')
    s = s.replace('class ', '').replace('struct ', '')
    return s

def _op_dynamic_type(pop):
    try:
        if pop == 0:
            return "<NULL>"
        return shorten_type_name(pop.dereference().dynamic_type)
    except Exception:
        return shorten_type_name(pop.type)


def get_scalar_const_value_str(pop):
    """从 CScalarConst 算子中提取值字符串"""
    try:
        # 1. 先把 pop 转为算子类 CScalarConst，以便访问它的成员
        scalar_const_ptr = pop.cast(gdb.lookup_type("gpopt::CScalarConst").pointer())

        # 2. 获取算子内部持有的 m_pdatum 指针
        p_datum = scalar_const_ptr['m_pdatum']
        if p_datum == 0:
            return "NULL"

        # 3. 获取该 Datum 的动态类型名称
        dynamic_type = _op_dynamic_type(p_datum)

        # 4. 根据类型进行处理
        if "CDatumInt4" in dynamic_type:
            # 注意：这里是对 p_datum 进行转换，而不是对 pop
            # 命名空间可能是 gpnaucrates 或 gpdxl，根据你报错的信息选择
            specific_datum = p_datum.cast(gdb.lookup_type("gpnaucrates::CDatumInt4GPDB").pointer())

            # 5. 关键：将 gdb.Value 显式转为 Python int
            return str(int(specific_datum['m_val']))

        elif "CDatumBool" in dynamic_type:
            specific_datum = p_datum.cast(gdb.lookup_type("gpnaucrates::CDatumBoolGPDB").pointer())
            # 布尔值处理
            return "true" if bool(specific_datum['m_value']) else "false"

        # 如果是其他类型，依然使用你之前的 Pstr() 方案作为兜底，最稳妥
        return get_cw_string(p_datum.cast(gdb.lookup_type("gpopt::IDatum").pointer())['Pstr']().dereference())

    except Exception as e:
        return f"<error: {e}>"

# --------- printer for gpopt::CExpression ----------
# 输出格式照搬 ORCA 自己的 CExpression::OsPrint（"Algebrized preprocessed query" 那种）：
#   +--CLogicalSelect
#      |--CLogicalGet "o" ("o"), Columns: ["k" (0), ...] Key sets: {[2,8]}
#      +--CScalarCmp (=)
# 每个算子的那一行按各自的 COperator::OsPrint 拼出来，但只读内存、不调用 inferior 函数。

_ptr_type_cache = {}

def _cast_to(ptr, cls):
    """把指针转为 <cls>*（不带命名空间时默认 gpopt::）并解引用。"""
    t = _ptr_type_cache.get(cls)
    if t is None:
        t = gdb.lookup_type(cls if '::' in cls else "gpopt::" + cls).pointer()
        _ptr_type_cache[cls] = t
    return ptr.cast(t).dereference()

def _ptr_elems(arr_ptr):
    """CDynamicPtrArray<T>* -> [T*, ...]"""
    if arr_ptr == 0:
        return []
    arr = arr_ptr.dereference()
    elems = arr['m_elems']
    return [(elems + i).dereference() for i in range(array_size(arr))]

def _os_name(cname):
    """CName / CName* -> "name"（CName::OsPrint 带双引号）"""
    return '"' + get_cw_string(cname['m_str_name']) + '"'

def _os_colref(pcr):
    """CColRef::OsPrint: "name" (id)"""
    try:
        if pcr == 0:
            return "NULL"
        cr = pcr.dereference()
        return f"{_os_name(cr['m_pname'])} ({int(cr['m_id'])})"
    except Exception:
        return "?"

def _os_colrefs(arr_ptr):
    """CUtils::OsPrintDrgPcr"""
    try:
        return ", ".join(_os_colref(p) for p in _ptr_elems(arr_ptr))
    except Exception as e:
        return f"<error: {e}>"

def _os_wstr(p):
    """CWStringBase* -> GetBuffer()"""
    try:
        return "" if p == 0 else get_cw_string(p.dereference())
    except Exception:
        return "?"

def _os_mdid(pmdid):
    """CMDIdGPDB::OsPrint: (type.oid.major.minor)"""
    try:
        if pmdid == 0:
            return "(NULL)"
        m = _cast_to(pmdid, "gpmd::CMDIdGPDB")
        return (f"({int(m['m_sysid']['m_mdid_type'])}.{int(m['m_oid'])}."
                f"{int(m['m_major_version'])}.{int(m['m_minor_version'])})")
    except Exception:
        return "(?)"

def _mdid_oid(pmdid):
    try:
        return None if pmdid == 0 else int(_cast_to(pmdid, "gpmd::CMDIdGPDB")['m_oid'])
    except Exception:
        return None

_TEXT_OIDS = {25, 1042, 1043, 19, 18}                      # IsTextRelated
_LINT_OIDS = {2950, 790}                                   # uuid, cash
_DOUBLE_OIDS = {1700, 700, 701,                            # numeric, float4, float8
                1082, 1083, 1266, 1114, 1184, 702, 703, 1186, 704,   # 时间类
                869, 650, 829}                             # inet, cidr, macaddr

def _decode_text_datum(d, oid):
    size = int(d['m_size'])
    if size <= 0 or size > 1 << 20:
        return None
    raw = bytes(gdb.selected_inferior().read_memory(d['m_bytearray_value'], min(size, 4096)))
    if oid == 18:
        return raw[:1].decode('latin-1')
    if oid == 19:
        return raw.split(b'\0', 1)[0].decode('utf-8', 'replace')
    if raw[0] & 0x01:                                       # varlena 1 字节短头
        body = raw[1:(raw[0] >> 1)]
    else:                                                   # varlena 4 字节头
        body = raw[4:(int.from_bytes(raw[:4], 'little') >> 2)]
    return body.decode('utf-8', 'replace')

def _os_datum(pdatum):
    """IDatum::OsPrint"""
    try:
        if pdatum == 0:
            return "NULL"
        dt = _op_dynamic_type(pdatum)
        for cls in ("CDatumInt2GPDB", "CDatumInt4GPDB", "CDatumInt8GPDB", "CDatumOidGPDB"):
            if cls in dt:
                d = _cast_to(pdatum, "gpnaucrates::" + cls)
                return "null" if bool(d['m_is_null']) else str(int(d['m_val']))
        if "CDatumBoolGPDB" in dt:
            d = _cast_to(pdatum, "gpnaucrates::CDatumBoolGPDB")
            return "null" if bool(d['m_is_null']) else str(int(bool(d['m_value'])))
        if "CDatumGenericGPDB" in dt:
            # CDatumGenericGPDB::GetStrRepr
            d = _cast_to(pdatum, "gpnaucrates::CDatumGenericGPDB")
            if bool(d['m_is_null']):
                return "null"
            oid = _mdid_oid(d['m_mdid'])
            if oid in _TEXT_OIDS:
                # ORCA 这里打印的是文本的 LINT 哈希（%0.3f），没有可读性；改为打印原文
                s = _decode_text_datum(d, oid)
                if s is not None:
                    return "'" + s + "'"
            if oid in _TEXT_OIDS or oid in _LINT_OIDS:
                return "%0.3f" % float(int(d['m_stats_comp_val_int']))
            if oid in _DOUBLE_OIDS:
                return "%0.3f" % float(d['m_stats_comp_val_double']['m_d'])
            size = int(d['m_size'])
            raw = bytes(gdb.selected_inferior().read_memory(d['m_bytearray_value'], min(size, 4096)))
            return raw.hex().upper()
        return f"<{dt}>"
    except Exception as e:
        return f"<error: {e}>"

def _os_orderspec(pos):
    """COrderSpec::OsPrint"""
    try:
        oes = [safe_deref(p) for p in _ptr_elems(pos.dereference()['m_pdrgpoe'])]
        if not oes:
            return "<empty>"
        nulls = {0: "Auto", 1: "NULLsFirst", 2: "NULLsLast"}
        return "".join(f"( {_os_mdid(oe['m_mdid'])}, {_os_colref(oe['m_pcr'])}, "
                       f"{nulls.get(int(oe['m_ent']), '?')} ) " for oe in oes if oe is not None)
    except Exception as e:
        return f"<error: {e}>"

def _os_distspec(pds, prefix=""):
    """CDistributionSpec*::OsPrint / CDistributionSpecHashed::OsPrintWithPrefix"""
    try:
        if pds == 0:
            return "NULL"
        t = _op_dynamic_type(pds).split("::")[-1]
        if t in ("CDistributionSpecHashed", "CDistributionSpecStrictHashed", "CDistributionSpecHashedNoOp"):
            szid = {"CDistributionSpecHashed": "HASHED", "CDistributionSpecStrictHashed": "STRICT HASHED",
                    "CDistributionSpecHashedNoOp": "HASHED NO-OP"}[t]
            d = _cast_to(pds, "CDistributionSpecHashed")
            exprs = [p.dereference() for p in _ptr_elems(d['m_pdrgpexpr'])]
            s = f"{szid}: [ "
            for e in exprs:
                pop = e['m_pop']
                et = _op_dynamic_type(pop).split("::")[-1]
                if len(exprs) == 1 and et == "CScalarIdent":
                    s += _os_op(e, pop, et)
                else:                      # os << *(hash_expr)：整棵子树，每行以换行结尾
                    lines = []
                    _os_expr_lines(e, "", True, 0, lines)
                    s += "\n".join(lines) + "\n" + prefix
            s += ", nulls colocated" if bool(d['m_fNullsColocated']) else ", nulls not colocated"
            if bool(d['m_is_duplicate_sensitive']):
                s += ", duplicate sensitive"
            if not bool(d['m_fSatisfiedBySingleton']):
                s += ", across-segments"
            s += " ]"
            if d['m_opfamilies'] != 0:
                fams = _ptr_elems(d['m_opfamilies'])
                if fams:
                    s += ", opfamilies: " + "".join(_os_mdid(f) + "," for f in fams)
            return s
        if t in ("CDistributionSpecSingleton", "CDistributionSpecStrictSingleton"):
            seg = "coordinator" if int(_cast_to(pds, "CDistributionSpecSingleton")['m_est']) == 0 else "segment"
            return ("STRICT SINGLETON" if "Strict" in t else "SINGLETON") + f" ({seg})"
        if t == "CDistributionSpecUniversal":
            return "UNIVERSAL "
        if t == "CDistributionSpecNonSingleton":
            s = "NON-SINGLETON "
            if not bool(_cast_to(pds, t)['m_fAllowReplicated']):
                s += " (NON-REPLICATED)"
            return s
        if t == "CDistributionSpecRandom":
            return "RANDOM"
        if t == "CDistributionSpecStrictRandom":
            return "STRICT RANDOM"
        if t == "CDistributionSpecAny":
            return f"ANY  EOperatorId: {int(_cast_to(pds, t)['m_eopidRequested'])} "
        if t == "CDistributionSpecNonReplicated":
            return "NON-REPLICATED"
        if t == "CDistributionSpecReplicated":
            r = str(_cast_to(pds, t)['m_replicated']).split("::")[-1]
            return {"EdtReplicated": "REPLICATED", "EdtTaintedReplicated": "TAINTED REPLICATED",
                    "EdtStrictReplicated": "STRICT REPLICATED"}.get(r, r)
        if t == "CDistributionSpecRouted":
            return f"ROUTED: [ {_os_colref(_cast_to(pds, t)['m_pcrSegmentId'])} ]"
        return t
    except Exception as e:
        return f"<error: {e}>"

_FRAME_BOUNDARY = ["Unbounded Preceding", "Bounded Preceding", "Current", "Unbounded Following",
                   "Bounded Following", "Delayed Bounded Preceding", "Delayed Bounded Following"]
_FRAME_EXCLUSION = ["None", "Nulls", "Current", "MatchingOthers", "Ties"]

def _os_windowframes(arr_ptr):
    """CWindowFrame::OsPrint(os, array)"""
    try:
        out = []
        for p in _ptr_elems(arr_ptr):
            wf = p.dereference()
            efs = _enum_short(wf['m_efs'], "Efs")
            if efs == "Sentinel":
                out.append("EMPTY FRAME")
                continue
            def bound(fld, efld):
                s = _FRAME_BOUNDARY[int(wf[fld])]
                if wf[efld] != 0:                 # os << " " << *pexpr：整棵子树
                    lines = []
                    _os_expr_lines(wf[efld].dereference(), "", True, 0, lines)
                    s += " " + "\n".join(lines) + "\n"
                return s
            out.append(f"[{efs}, Trail: {bound('m_efbTrailing', 'm_pexprTrailing')}, "
                       f"Lead: {bound('m_efbLeading', 'm_pexprLeading')}, "
                       f"{_FRAME_EXCLUSION[int(wf['m_efes'])]}]")
        return "[" + ", ".join(out) + "]"
    except Exception as e:
        return f"<error: {e}>"

def _enum_short(v, prefix):
    s = str(v).split("::")[-1]
    return s[len(prefix):] if s.startswith(prefix) else s

def _os_tabdesc_name(ptabdesc):
    """CTableDescriptor* 或 CTableDescriptorHashSet*（取第一个）的 Name()"""
    if ptabdesc == 0:
        return '""'
    td = ptabdesc
    if _has_field(td.dereference(), 'm_elements'):
        elems = _ptr_elems(td.dereference()['m_elements'])
        if not elems:
            return '""'
        td = elems[0]
    return _os_name(td.dereference()['m_name'])

def _os_keysets(ptabdesc, ncols):
    td = ptabdesc
    if _has_field(td.dereference(), 'm_elements'):
        td = _ptr_elems(td.dereference()['m_elements'])[0]
    sets = []
    for pbs in _ptr_elems(td.dereference()['m_pdrgpbsKeys']):
        bits = [b for b in bitset_members(pbs.dereference()) if b < ncols]
        sets.append("[" + ",".join(str(b) for b in bits) + "]")
    return "{" + ", ".join(sets) + "}"

_GBAGG_TYPE = {0: "Global", 1: "Local", 2: "Intermediate"}

def _os_op(val, pop, t):
    """COperator::OsPrint 的各个 override"""
    try:
        if bool(pop.dereference()['m_fPattern']):
            return t
    except Exception:
        pass
    try:
        if t in ("CLogicalGet", "CLogicalForeignGet"):
            op = _cast_to(pop, "CLogicalGet")
            cols = _ptr_elems(op['m_pdrgpcrOutput'])
            return (f"{t} {_os_name(op['m_pnameAlias'])} ({_os_tabdesc_name(op['m_ptabdesc'])}), "
                    f"Columns: [{_os_colrefs(op['m_pdrgpcrOutput'])}] "
                    f"Key sets: {_os_keysets(op['m_ptabdesc'], len(cols))}")
        if t in ("CLogicalDynamicGet", "CLogicalDynamicForeignGet"):
            op = _cast_to(pop, "CLogicalDynamicGetBase")
            nparts = array_size(op['m_partition_mdids'].dereference()) if op['m_partition_mdids'] != 0 else 0
            return (f"{t} {_os_name(op['m_pnameAlias'])} ({_os_tabdesc_name(op['m_ptabdesc'])}), "
                    f"Columns: [{_os_colrefs(op['m_pdrgpcrOutput'])}] Scan Id: {int(op['m_scan_id'])} "
                    f"Parts to scan: {nparts}")
        if t in ("CLogicalIndexGet", "CLogicalIndexOnlyGet"):
            op = _cast_to(pop, "CLogicalIndexGet")
            s = (f"{t}   Index Name: ({_os_name(op['m_pindexdesc']['m_name'])}), "
                 f"Table Name: ({_os_name(op['m_pnameAlias'])}), Columns: [{_os_colrefs(op['m_pdrgpcrOutput'])}]")
            if "Backward" in str(op['m_scan_direction']):
                s += ", Backward Scan"
            return s
        if t in ("CLogicalDynamicIndexGet", "CLogicalDynamicIndexOnlyGet"):
            op = _cast_to(pop, "CLogicalDynamicIndexGet")
            base = _cast_to(pop, "CLogicalDynamicGetBase")
            return (f"{t}   Index Name: ({_os_name(op['m_pindexdesc']['m_name'])}), "
                    f"Table Name: ({_os_name(base['m_pnameAlias'])}), "
                    f"Columns: [{_os_colrefs(base['m_pdrgpcrOutput'])}] Scan Id: {int(base['m_scan_id'])}")
        if t == "CLogicalBitmapTableGet":
            op = _cast_to(pop, t)
            return (f"{t} , Table Name: ({_os_tabdesc_name(op['m_ptabdesc'])}), "
                    f"Columns: [{_os_colrefs(op['m_pdrgpcrOutput'])}]")
        if t == "CLogicalDynamicBitmapTableGet":
            op = _cast_to(pop, "CLogicalDynamicGetBase")
            return (f"{t} , Table Name: ({_os_tabdesc_name(op['m_ptabdesc'])}) Scan Id: {int(op['m_scan_id'])}, "
                    f"Columns: [{_os_colrefs(op['m_pdrgpcrOutput'])}]")
        if t == "CLogicalConstTableGet":
            op = _cast_to(pop, t)
            rows = []
            for prow in _ptr_elems(op['m_pdrgpdrgpdatum']):
                rows.append("(" + ", ".join(_os_datum(d) for d in _ptr_elems(prow)) + ")")
            return f"{t} Columns: [{_os_colrefs(op['m_pdrgpcrOutput'])}] Values: [{'; '.join(rows)}]"
        if t in ("CLogicalGbAgg", "CLogicalGbAggDeduplicate"):
            op = _cast_to(pop, "CLogicalGbAgg")
            gtype = _GBAGG_TYPE.get(int(op['m_egbaggtype']), "?")
            minimal = _os_colrefs(op['m_pdrgpcrMinimal']) if op['m_pdrgpcrMinimal'] != 0 else ""
            dup = int(bool(op['m_fGeneratesDuplicates']))
            if t == "CLogicalGbAggDeduplicate":
                keys = _os_colrefs(_cast_to(pop, t)['m_pdrgpcrKeys'])
                return (f"{t}( {gtype} ) Grp Cols: [{_os_colrefs(op['m_pdrgpcr'])}], Minimal Grp Cols: [{minimal}], "
                        f"Join Child Keys: [{keys}], Generates Duplicates :[ {dup} ] ")
            s = (f"{t}( {gtype} ) Grp Cols: [{_os_colrefs(op['m_pdrgpcr'])}][{gtype}], "
                 f"Minimal Grp Cols: [{minimal}]")
            if gtype == "Intermediate":
                s += f", Distinct Cols:[{_os_colrefs(op['m_pdrgpcrArgDQA'])}]"
            s += f", Generates Duplicates :[ {dup} ] "
            stage = int(op['m_aggStage'])
            if stage == 0:
                s += ", m_aggStage :[  Two Stage Scalar DQA  ] "
            elif stage == 1:
                s += ", m_aggStage :[  Three Stage Scalar DQA  ] "
            return s
        if t == "CLogicalLimit":
            op = _cast_to(pop, t)
            return (f"{t} {_os_orderspec(op['m_pos'])} {'global' if bool(op['m_fGlobal']) else 'local'}"
                    + (" NonRemovableLimit" if bool(op['m_top_limit_under_dml']) else ""))
        if t == "CLogicalNAryJoin":
            op = _cast_to(pop, t)
            if op['m_lojChildPred'] == 0:
                return t
            vals = [str(int(p.dereference())) for p in _ptr_elems(op['m_lojChildPred'])]
            return f"{t} [{', '.join(vals)}]"
        if t.startswith("CLogical") and "Apply" in t:
            op = _cast_to(pop, "CLogicalApply")
            if op['m_pdrgpcrInner'] == 0:
                return t
            return f"{t} (Reqd Inner Cols: {_os_colrefs(op['m_pdrgpcrInner'])})"
        if t in ("CLogicalUnion", "CLogicalUnionAll", "CLogicalIntersect", "CLogicalIntersectAll",
                 "CLogicalDifference", "CLogicalDifferenceAll"):
            op = _cast_to(pop, "CLogicalSetOp")
            ins = ", ".join(f"({_os_colrefs(p)})" for p in _ptr_elems(op['m_pdrgpdrgpcrInput']))
            return f"{t} Output: ({_os_colrefs(op['m_pdrgpcrOutput'])}), Input: [{ins}]"
        if t in ("CLogicalCTEConsumer", "CLogicalCTEProducer"):
            op = _cast_to(pop, t)
            return f"{t} ({int(op['m_id'])}), Columns: [{_os_colrefs(op['m_pdrgpcr'])}]"
        if t == "CLogicalCTEAnchor":
            return f"{t} ({int(_cast_to(pop, t)['m_id'])})"
        if t == "CLogicalSequenceProject":
            op = _cast_to(pop, t)
            ords = "[" + ", ".join(_os_orderspec(p) for p in _ptr_elems(op['m_pdrgpos'])) + "]"
            return (f"{t} (Partition By Keys:{_os_distspec(op['m_pds'])}, Order Spec:{ords}, "
                    f"WindowFrame Spec:{_os_windowframes(op['m_pdrgpwf'])})")
        if t == "CLogicalInsert":
            op = _cast_to(pop, t)
            return (f"{t} ({_os_tabdesc_name(op['m_ptabdesc'])}), "
                    f"Source Columns: [{_os_colrefs(op['m_pdrgpcrSource'])}]")

        # ---- physical ----
        if t.startswith("CPhysical"):
            r = _os_physical(pop, t)
            if r is not None:
                return r

        # ---- scalar ----
        if t == "CScalarIdent":
            return f"{t} {_os_colref(_cast_to(pop, t)['m_pcr'])}"
        if t == "CScalarProjectElement":
            return f"{t} {_os_colref(_cast_to(pop, t)['m_pcr'])}"
        if t == "CScalarConst":
            return f"{t} ({_os_datum(_cast_to(pop, t)['m_pdatum'])})"
        if t in ("CScalarCmp", "CScalarIsDistinctFrom"):
            return f"{t} ({_os_wstr(_cast_to(pop, 'CScalarCmp')['m_pstrOp'])})"
        if t == "CScalarOp":
            return f"{t} ({_os_wstr(_cast_to(pop, t)['m_pstrOp'])})"
        if t == "CScalarBoolOp":
            return f"{t} ({str(_cast_to(pop, t)['m_eboolop']).split('::')[-1]})"
        if t == "CScalarBooleanTest":
            words = ["Is True", "Is Not True", "Is False", "Is Not False", "Is Unknown", "Is Not Unknown"]
            i = int(_cast_to(pop, t)['m_ebt'])
            return f"{t} ({words[i] if 0 <= i < len(words) else '?'})"
        if t == "CScalarBitmapBoolOp":
            i = int(_cast_to(pop, t)['m_ebitmapboolop'])
            return f"{t} ({['BitmapAnd', 'BitmapOr'][i] if i in (0, 1) else '?'})"
        if t == "CScalarFunc":
            return f"{t} ({_os_wstr(_cast_to(pop, t)['m_pstrFunc'])})"
        if t == "CScalarWindowFunc":
            op = _cast_to(pop, t)
            name = _os_wstr(_cast_to(pop, "CScalarFunc")['m_pstrFunc'])
            b = lambda f: "true" if bool(op[f]) else "false"
            return (f"{t} ({name} , Agg: {b('m_fAgg')} , Distinct: {b('m_is_distinct')} , "
                    f"StarArgument: {b('m_is_star_arg')} , SimpleAgg: {b('m_is_simple_agg')})")
        if t == "CScalarAggFunc":
            op = _cast_to(pop, t)
            stage = {0: "Global", 1: "Intermediate", 2: "Local"}.get(int(op['m_eaggfuncstage']), "?")
            return (f"{t} ({_os_wstr(op['m_pstrAggFunc'])} , Distinct: "
                    f"{'true' if bool(op['m_is_distinct']) else 'false'} , Aggregate Stage: {stage})")
        if t == "CScalarArrayCmp":
            op = _cast_to(pop, t)
            kind = "Any" if int(op['m_earrccmpt']) == 0 else "All"
            return f"{t} {kind} ({_os_wstr(op['m_pscOp'])})"
        if t == "CScalarArray":
            op = _cast_to(pop, t)
            s = f"CScalarArray: {{eleMDId: {_os_mdid(op['m_pmdidElem'])}, arrayMDId: {_os_mdid(op['m_pmdidArray'])}"
            if bool(op['m_fMultiDimensional']):
                s += ", multidimensional"
            for pc in _ptr_elems(op['m_pdrgPconst']):
                s += f" CScalarConst ({_os_datum(pc.dereference()['m_pdatum'])})"
            return s + "}"
        if t == "CScalarMinMax":
            return f"{t} ({'Min' if int(_cast_to(pop, t)['m_esmmt']) == 0 else 'Max'})"
        if t == "CScalarParam":
            return f"{t} ({int(_cast_to(pop, t)['m_id'])})"
        if t == "CScalarSortGroupClause":
            op = _cast_to(pop, t)
            b = lambda f: "true" if bool(op[f]) else "false"
            return (f"{t}(tleSortGroupRef:{int(op['m_tle_sort_group_ref'])},eqop:{int(op['m_eqop'])},"
                    f"sortop:{int(op['m_sortop'])},nulls_first:{b('m_nulls_first')},hashable:{b('m_hashable')})")
        if t == "CScalarSubquery":
            op = _cast_to(pop, t)
            s = f"{t}[{_os_colref(op['m_pcr'])}]"
            if bool(op['m_fGeneratedByExist']):
                s += " generated by Exist SQ"
            if bool(op['m_fGeneratedByQuantified']):
                s += " generated by Quantified SQ"
            return s
        if t in ("CScalarSubqueryAny", "CScalarSubqueryAll"):
            op = _cast_to(pop, "CScalarSubqueryQuantified")
            return f"{t}({_os_wstr(op['m_pstrScalarOp'])})[{_os_colref(op['m_pcr'])}]"
        if t == "CScalarAssertConstraint":
            return f"{t} (ErrorMsg: {_os_wstr(_cast_to(pop, t)['m_pstrErrorMsg'])})"
    except Exception as e:
        return f"{t} <error: {e}>"
    return t

def _os_physical(pop, t):
    if t in ("CPhysicalScalarAgg", "CPhysicalHashAgg", "CPhysicalStreamAgg"):
        op = _cast_to(pop, "CPhysicalAgg")
        gtype = _GBAGG_TYPE.get(int(op['m_egbaggtype']), "?")
        s = f"{t}( {gtype}" + (", multi-stage" if bool(op['m_fMultiStage']) else "") + " )"
        s += f" Grp Cols: [{_os_colrefs(op['m_pdrgpcr'])}], Minimal Grp Cols:[{_os_colrefs(op['m_pdrgpcrMinimal'])}]"
        if gtype == "Intermediate":
            s += f", Distinct Cols:[{_os_colrefs(op['m_pdrgpcrArgDQA'])}]"
        s += f", Generates Duplicates :[ {int(bool(op['m_fGeneratesDuplicates']))} ] "
        stage = int(op['m_aggStage'])
        if stage == 0:
            s += ", m_aggStage :[ Two Stage Scalar DQA ] "
        elif stage == 1:
            s += ", m_aggStage :[ Three Stage Scalar DQA ] "
        return s
    if t in ("CPhysicalHashAggDeduplicate", "CPhysicalStreamAggDeduplicate"):
        op = _cast_to(pop, "CPhysicalAgg")
        keys = _os_colrefs(_cast_to(pop, t)['m_pdrgpcrKeys'])
        return (f"{t}( {_GBAGG_TYPE.get(int(op['m_egbaggtype']), '?')} ) Grp Cols: [{_os_colrefs(op['m_pdrgpcr'])}]"
                f", Key Cols:[{keys}], Generates Duplicates :[ {int(bool(op['m_fGeneratesDuplicates']))} ] ")
    if t in ("CPhysicalTableScan", "CPhysicalForeignScan"):
        op = _cast_to(pop, "CPhysicalScan")
        return f"{t} {_os_name(op['m_pnameAlias'])} ({_os_tabdesc_name(op['m_ptabdesc'])})"
    if t in ("CPhysicalDynamicTableScan", "CPhysicalDynamicForeignScan"):
        op = _cast_to(pop, "CPhysicalDynamicScan")
        sc = _cast_to(pop, "CPhysicalScan")
        nparts = array_size(op['m_partition_mdids'].dereference()) if op['m_partition_mdids'] != 0 else 0
        return (f"{t} {_os_name(sc['m_pnameAlias'])} ({_os_tabdesc_name(sc['m_ptabdesc'])}), "
                f"Columns: [{_os_colrefs(sc['m_pdrgpcrOutput'])}] Scan Id: {int(op['m_scan_id'])} "
                f"Parts to scan: {nparts}")
    if t in ("CPhysicalIndexScan", "CPhysicalIndexOnlyScan"):
        op = _cast_to(pop, t)
        sc = _cast_to(pop, "CPhysicalScan")
        s = (f"{t}   Index Name: ({_os_name(op['m_pindexdesc']['m_name'])}), "
             f"Table Name: ({_os_tabdesc_name(sc['m_ptabdesc'])}), Columns: [{_os_colrefs(sc['m_pdrgpcrOutput'])}]")
        if "Backward" in str(op['m_scan_direction']):
            s += ", Backward Scan"
        return s
    if t in ("CPhysicalDynamicIndexScan", "CPhysicalDynamicIndexOnlyScan"):
        op = _cast_to(pop, "CPhysicalDynamicIndexScan")
        dyn = _cast_to(pop, "CPhysicalDynamicScan")
        sc = _cast_to(pop, "CPhysicalScan")
        return (f"{t}   Index: ({_os_name(op['m_pindexdesc']['m_name'])}), "
                f"Table: ({_os_tabdesc_name(sc['m_ptabdesc'])}), Columns: [{_os_colrefs(sc['m_pdrgpcrOutput'])}]"
                f" Scan Id: {int(dyn['m_scan_id'])}")
    if t in ("CPhysicalBitmapTableScan", "CPhysicalDynamicBitmapTableScan"):
        sc = _cast_to(pop, "CPhysicalScan")
        return (f"{t} , Table Name: ({_os_tabdesc_name(sc['m_ptabdesc'])}), "
                f"Columns: [{_os_colrefs(sc['m_pdrgpcrOutput'])}]")
    if t == "CPhysicalLimit":
        op = _cast_to(pop, t)
        return f"{t} {_os_orderspec(op['m_pos'])} {'global' if bool(op['m_fGlobal']) else 'local'}"
    if t == "CPhysicalMotionGather":
        op = _cast_to(pop, t)
        est = int(op['m_pdssSingeton'].dereference()['m_est'])
        s = t + ("(coordinator)" if est == 0 else "(segment)")
        if op['m_pos'] != 0 and _ptr_elems(op['m_pos'].dereference()['m_pdrgpoe']):
            s += _os_orderspec(op['m_pos'])
        return s
    if t == "CPhysicalMotionBroadcast":
        return t + " "
    if t == "CPhysicalMotionHashDistribute":
        op = _cast_to(pop, t)
        return f"{t} " + _os_distspec(op['m_pdsHashed'], " " * 40)
    if t == "CPhysicalMotionRandom":
        op = _cast_to(pop, t)
        dup = op['m_pdsRandom'] != 0 and bool(op['m_pdsRandom'].dereference()['m_is_duplicate_sensitive'])
        return t + (" (dup sensitive) " if dup else "")
    if t == "CPhysicalSpool":
        return f"{t} (" + ("Blocking)" if bool(_cast_to(pop, t)['m_eager']) else "Streaming)")
    if t == "CPhysicalSort":
        return f"{t}  " + _os_orderspec(_cast_to(pop, t)['m_pos'])
    if t == "CPhysicalSequenceProject":
        op = _cast_to(pop, t)
        ords = "[" + ", ".join(_os_orderspec(p) for p in _ptr_elems(op['m_pdrgpos'])) + "]"
        return f"{t} ({_os_distspec(op['m_pds'])}, {ords}, {_os_windowframes(op['m_pdrgpwf'])})"
    if t.startswith("CPhysicalCorrelated") and t.endswith("NLJoin"):
        op = _cast_to(pop, t)
        return f"{t}({_os_colrefs(op['m_pdrgpcrInner'])})"
    if t == "CPhysicalPartitionSelector":
        op = _cast_to(pop, t)
        return (f"{t}, Id: {int(op['m_selector_id'])}, Scan Id: {int(op['m_scan_id'])}, "
                f"Part Table: {_os_mdid(op['m_mdid'])}")
    if t == "CPhysicalConstTableGet":
        op = _cast_to(pop, t)
        rows = ["(" + ", ".join(_os_datum(d) for d in _ptr_elems(prow)) + ")"
                for prow in _ptr_elems(op['m_pdrgpdrgpdatum'])]
        return f"{t} Columns: [{_os_colrefs(op['m_pdrgpcrOutput'])}] Values: [{'; '.join(rows)}]"
    if t in ("CPhysicalCTEConsumer", "CPhysicalCTEProducer"):
        op = _cast_to(pop, t)
        return f"{t} ({int(op['m_id'])}), Columns: [{_os_colrefs(op['m_pdrgpcr'])}]"
    return None

def _os_stats(val):
    """CExpression::OsPrintExpression 里非标量算子的 rows/width/rebinds"""
    try:
        ps = val['m_pstats']
        if ps == 0:
            return ""
        st = _cast_to(ps, "gpnaucrates::CStatistics")
        rows = int(float(st['m_rows']['m_d']))
        width = 0.0
        if st['m_colid_width_mapping'] != 0:
            for _, v in _hashmap_items(st['m_colid_width_mapping'].dereference()):
                width += float(v.dereference()['m_d'])
        import math
        rebinds = int(float(st['m_num_rebinds']['m_d']))
        if width <= 0:
            width = 1e-300       # CDouble 会把 0 夹到 GPOS_FP_ABS_MIN，Ceil() 之后是 1
        return f"   rows:{rows}   width:{int(math.ceil(width))}  rebinds:{rebinds}"
    except Exception:
        return ""

_ULONG_MAX = 0xFFFFFFFF

def print_cexpression(val, depth=0, suffix="", ctx=None, path=()):
    # depth 只决定整棵树的起始缩进（供其它打印函数嵌套调用）；suffix 保留参数兼容，不再使用
    # ctx: _ExprCtx（-p / --check / -n 时才有）；path: val 在 ctx.root 里的路径
    lines = []
    _os_expr_lines(val, mkpad(depth), True, 0, lines, ctx, tuple(path))
    for l in lines:
        print(l)

def _os_expr_lines(val, prefix, last, level, out, ctx=None, path=()):
    """CExpression::OsPrintExpression；ctx 非空时追加 path / !! / 属性行"""
    line = prefix + ("+--" if last else "|--")
    child_prefix = prefix + ("   " if last else "|  ")
    if level > MAX_DEPTH:
        out.append(line + f"[warn] depth > {MAX_DEPTH}, stop (cycle or garbage?)")
        return
    pop, t = _expr_op(val)
    if pop is None:
        out.append(line + "<NULL operator>")
        return

    s = _os_op(val, pop, t)
    if not t.startswith("CScalar"):
        s += _os_stats(val)
    if t.startswith("CPhysical"):
        try:
            s += "   cost:%f" % float(val['m_cost']['m_d'])        # COstream << DOUBLE 用 %f
        except Exception:
            pass
    try:
        grp = int(val['m_ulOriginGrpId'])
        if grp != _ULONG_MAX:
            s += f"   origin: [Grp:{grp}, GrpExpr:{int(val['m_ulOriginGrpExprId'])}]"
    except Exception:
        pass
    if ctx is not None:
        if ctx.paths:
            s += "   " + _path_str(path)
        for msg in ctx.flags.get(path, ()):
            s += "   !! " + msg
    out.append(line + s)

    try:
        children = _expr_children(val)
    except Exception as e:
        out.append(child_prefix + f"+--[warn] child access error: {e}")
        return
    if ctx is not None and ctx.props:
        pp = child_prefix + ("|  " if children else "   ")
        for pl in _props_lines(val, t, ctx):
            out.append(pp + pl)
    for i, c in enumerate(children):
        if c is None:
            out.append(child_prefix + ("+--" if i == len(children) - 1 else "|--") + "<NULL child>")
            continue
        _os_expr_lines(c, child_prefix, i == len(children) - 1, level + 1, out, ctx, path + (i,))

# ---------- 树遍历 / 路径 (@1.0 = 根的第 1 个孩子的第 0 个孩子) ----------

def _expr_op(val):
    pop = val['m_pop'] if _has_field(val, 'm_pop') else 0
    if pop == 0:
        return None, None
    return pop, _op_dynamic_type(pop).split("::")[-1]

def _expr_children(val):
    """孩子列表；空指针孩子以 None 占位，保证下标和 ORCA 一致"""
    if not _has_field(val, 'm_pdrgpexpr') or val['m_pdrgpexpr'] == 0:
        return []
    return [safe_deref(p) for p in _ptr_elems(val['m_pdrgpexpr'])]

def _path_str(path):
    return "@" + ".".join(str(i) for i in path) if path else "@root"

def _parse_path(s):
    s = s.lstrip("@")
    if s in ("", "root"):
        return ()
    try:
        return tuple(int(x) for x in s.split("."))
    except ValueError:
        raise gdb.GdbError(f"bad path '@{s}', expected e.g. @1.0.2")

def _subexpr(val, path):
    for k, i in enumerate(path):
        ch = _expr_children(val)
        if i >= len(ch) or ch[i] is None:
            raise gdb.GdbError(f"{_path_str(path[:k])} has no child {i} (arity {len(ch)})")
        val = ch[i]
    return val

def _walk(val, path=(), level=0):
    yield val, path
    if level > MAX_DEPTH:
        return
    for i, c in enumerate(_expr_children(val)):
        if c is not None:
            yield from _walk(c, path + (i,), level + 1)

# ---------- 算子里引用的列 ----------
# 扫描算子对象(含基类)中所有 CColRef* / CColRefArray* / CColRef2dArray* / CColRefSet* 成员，
# 不靠逐个算子手写，新算子也能覆盖。

_CR_FIELDS = {}
_CR_SKIP = {'m_pcrsLocalUsed'}      # CLogical 的缓存，不是算子语义的一部分

def _colref_fields(typ):
    typ = typ.strip_typedefs()
    key = str(typ)
    res = _CR_FIELDS.get(key)
    if res is not None:
        return res
    res = []
    def scan(t, seen):
        for f in t.fields():
            if f.is_base_class:
                bt = f.type.strip_typedefs()
                if str(bt) not in seen:
                    seen.add(str(bt))
                    scan(bt, seen)
                continue
            if not f.name or f.name in _CR_SKIP or not hasattr(f, 'bitpos'):
                continue                                    # 跳过 static 成员
            try:
                ft = f.type.strip_typedefs()
                if ft.code != gdb.TYPE_CODE_PTR:
                    continue
                tn = str(ft.target().unqualified().strip_typedefs())     # const CColRef *
            except Exception:
                continue
            if tn == 'gpopt::CColRef':
                res.append((f.name, 'one'))
            elif tn.startswith('gpos::CDynamicPtrArray<gpopt::CColRef,'):
                res.append((f.name, 'arr'))
            elif tn.startswith('gpos::CDynamicPtrArray<gpos::CDynamicPtrArray<gpopt::CColRef,'):
                res.append((f.name, 'arr2'))
            elif tn == 'gpopt::CColRefSet':
                res.append((f.name, 'set'))
    scan(typ, {key})
    _CR_FIELDS[key] = res
    return res

def _is_def_field(t, fname):
    """该成员是否“产生”列（其余都算引用）"""
    if fname in ('m_pdrgpcrOutput', 'm_pcrsOutput'):
        return True
    if fname == 'm_pcr' and t == 'CScalarProjectElement':
        return True
    if fname == 'm_pdrgpcr' and t in ('CLogicalCTEConsumer', 'CPhysicalCTEConsumer'):
        return True
    return False

def _node_colrefs(pop):
    """[(field, kind, [(id, name|None), ...]), ...]"""
    obj = _cast_to(pop, _op_dynamic_type(pop))
    out = []
    for fname, kind in _colref_fields(obj.type):
        try:
            v = obj[fname]
            if v == 0:
                continue
            if kind == 'set':
                out.append((fname, kind, [(i, None) for i in bitset_members(v.dereference())]))
                continue
            if kind == 'one':
                crs = [v]
            elif kind == 'arr':
                crs = _ptr_elems(v)
            else:
                crs = [c for a in _ptr_elems(v) if a != 0 for c in _ptr_elems(a)]
            ids = []
            for p in crs:
                if p == 0:
                    continue
                cr = p.dereference()
                ids.append((int(cr['m_id']), get_cw_string(cr['m_pname']['m_str_name'])))
            out.append((fname, kind, ids))
        except Exception:
            continue
    return out

# ---------- 已推导属性（只读 m_is_prop_derived 已置位的，不触发推导） ----------

def _drvd_bits(d, nbits):
    try:
        if bool(d['m_is_complete']):
            return set(range(nbits))
    except Exception:
        pass
    bs = d['m_is_prop_derived']
    return set(bitset_members(bs.dereference())) if bs != 0 else set()

def _crs_ids(p):
    return [] if p == 0 else bitset_members(p.dereference())

_MAXCARD_UNBOUNDED = 0xFFFFFFFF            # GPOPT_MAX_CARD = (ULLONG) gpos::ulong_max

def _drvd_rel(val):
    """CDrvdPropRelational；没推导过的键不出现。未分配返回 None"""
    try:
        p = val['m_pdprel']
        if p == 0:
            return None
        d = p.dereference()
        bits = _drvd_bits(d, 12)              # CDrvdPropRelational::EdptSentinel
        r = {}
        for bit, key, fld in ((0, 'out', 'm_pcrsOutput'), (1, 'outer', 'm_pcrsOuter'),
                              (2, 'notnull', 'm_pcrsNotNull'), (3, 'corr', 'm_pcrsCorrelatedApply')):
            if bit in bits:
                r[key] = _crs_ids(d[fld])
        if 4 in bits:
            r['keys'] = [] if d['m_pkc'] == 0 else \
                [_crs_ids(k) for k in _ptr_elems(d['m_pkc'].dereference()['m_pdrgpcrs'])]
        if 6 in bits:
            r['maxcard'] = int(d['m_maxcard']['m_ull'])
        if 10 in bits:
            r['joindepth'] = int(d['m_ulJoinDepth'])
        return r
    except Exception:
        return None

def _drvd_scalar(val):
    try:
        p = val['m_pdpscalar']
        if p == 0:
            return None
        d = p.dereference()
        bits = _drvd_bits(d, 13)              # CDrvdPropScalar::EdptSentinel
        r = {}
        for bit, key, fld in ((0, 'defined', 'm_pcrsDefined'), (1, 'used', 'm_pcrsUsed'),
                              (2, 'srf', 'm_pcrsSetReturningFunction')):
            if bit in bits:
                r[key] = _crs_ids(d[fld])
        if 3 in bits:
            r['subq'] = bool(d['m_fHasSubquery'])
        return r
    except Exception:
        return None

_RWD = {"ErtRewindable": "REWINDABLE", "ErtRescannable": "RESCANNABLE", "ErtNone": "NONE",
        "ErtMarkRestore": "MARK-RESTORE"}

def _os_rewind(prs):
    """CRewindabilitySpec::OsPrint"""
    try:
        if prs == 0:
            return "NULL"
        r = prs.dereference()
        s = _RWD.get(str(r['m_rewindability']).split("::")[-1], str(r['m_rewindability']))
        mh = str(r['m_motion_hazard']).split("::")[-1]
        return s + {"EmhtMotion": " MOTION", "EmhtNoMotion": " NO-MOTION"}.get(mh, "")
    except Exception as e:
        return f"<error: {e}>"

def _hashmap_values_ordered(hm_ptr):
    """CHashMapIter 的顺序：按 m_keys（插入顺序）取 value"""
    if hm_ptr == 0:
        return []
    hm = hm_ptr.dereference()
    d = {int(k): v for k, v in _hashmap_items(hm)}
    return [d[int(k)] for k in _ptr_elems(hm['m_keys']) if int(k) in d]

def _os_bitset(pbs):
    """CBitSet::OsPrint: {1, 2}"""
    return "{" + ", ".join(str(b) for b in _crs_ids(pbs)) + "}"

def _os_partprop(ppps):
    """CPartitionPropagationSpec::OsPrint"""
    try:
        if ppps == 0:
            return ""
        infos = _hashmap_values_ordered(ppps.dereference()['m_part_prop_spec_infos'])
        if not infos:
            return "<empty>"
        out = []
        for pi in infos:
            i = pi.dereference()
            ty = str(i['m_type']).split("::")[-1]
            kind = {"EpptConsumer": "consumer", "EpptPropagator": "propagator"}.get(ty, "")
            out.append(f"{kind}<{int(i['m_scan_id'])}>({_os_bitset(i['m_selector_ids'])})")
        return ", ".join(out)
    except Exception as e:
        return f"<error: {e}>"

def _is_cte_producer(ect):
    return str(ect).split("::")[-1] == "EctProducer"

def _os_ctemap(pcm):
    """CCTEMap::OsPrint"""
    try:
        if pcm == 0:
            return ""
        s = ""
        for pe in _hashmap_values_ordered(pcm.dereference()['m_phmcm']):
            e = pe.dereference()
            s += f"{int(e['m_id'])}{'p' if _is_cte_producer(e['m_ect']) else 'c'}"
            if e['m_pdpplan'] != 0:
                s += f"({_os_drvdplan(e['m_pdpplan'])})"
            s += " "
        return s
    except Exception as e:
        return f"<error: {e}>"

def _os_drvdplan(pdp):
    """CDrvdPropPlan::OsPrint"""
    d = pdp.dereference()
    return (f"Drvd Plan Props (ORD: {_os_orderspec(d['m_pos'])}, DIST: {_os_distspec(d['m_pds'])}, "
            f"REWIND: {_os_rewind(d['m_prs'])}, PART PROP:{_os_partprop(d['m_ppps'])}), "
            f"CTE Map: [{_os_ctemap(d['m_pcm'])}]")

def _os_ctereq(pcter):
    """CCTEReq::OsPrint"""
    try:
        s = ""
        for pe in _hashmap_values_ordered(pcter.dereference()['m_phmcter']):
            e = pe.dereference()
            s += (f"{int(e['m_id'])}{':p' if _is_cte_producer(e['m_ect']) else ':c'}"
                  f"{' ' if bool(e['m_fRequired']) else '(opt) '}")
            if e['m_pdpplan'] != 0:
                s += "Plan Props: " + _os_drvdplan(e['m_pdpplan'])
        return s
    except Exception as e:
        return f"<error: {e}>"

_DIST_MATCH = ["exact", "satisfy", "subset"]

def _os_reqdpropplan(prpp):
    """CReqdPropPlan::OsPrint（req cols 只在 trace flag 101016 打开时才打，这里同默认不打）"""
    if prpp == 0:
        return "NULL"
    r = prpp.dereference()
    s = "req CTEs: ["
    if r['m_pcter'] != 0:
        s += _os_ctereq(r['m_pcter'])
    s += "], req order: ["
    if r['m_peo'] != 0:
        s += _os_orderspec(r['m_peo'].dereference()['m_pos']) + " match: satisfy "
    s += "], req dist: ["
    if r['m_ped'] != 0:
        ed = r['m_ped'].dereference()
        m = int(ed['m_edm'])
        s += _os_distspec(ed['m_pds']) + " match: " + (_DIST_MATCH[m] if 0 <= m < 3 else str(m))
    s += "], req rewind: ["
    if r['m_per'] != 0:                     # ORCA 原样：这里把 "], req rewind: [" 又打了一遍
        s += "], req rewind: [" + _os_rewind(r['m_per'].dereference()['m_prs']) + " match: satisfy"
    s += "], req partition propagation: ["
    if r['m_pepp'] != 0:
        s += _os_partprop(r['m_pepp'].dereference()['m_ppps']) + " match: satisfy "
    return s + "]"

def print_cquerycontext(val):
    """CQueryContext::OsPrint: os << *m_pexpr << std::endl << *m_prpp"""
    pexpr = safe_deref(val['m_pexpr'])
    if pexpr is None:
        print("m_pexpr: NULL")
    else:
        print_cexpression(pexpr)
    print("")
    print(_os_reqdpropplan(val['m_prpp']))

def _one_line(s):
    return " ".join(x.strip() for x in s.split("\n") if x.strip())

def _ids_str(ids, ctx):
    return "[" + ", ".join(ctx.colname(i) for i in ids) + "]"

def _props_lines(val, t, ctx):
    lines = []
    if t.startswith("CScalar"):
        d = _drvd_scalar(val)
        if d:                                   # 标量节点很多，没推导过就不占行
            parts = []
            if d.get('used'):
                parts.append("used=" + _ids_str(d['used'], ctx))
            if d.get('defined'):
                parts.append("defined=" + _ids_str(d['defined'], ctx))
            if d.get('srf'):
                parts.append("srf=" + _ids_str(d['srf'], ctx))
            if d.get('subq'):
                parts.append("has-subquery")
            if parts:
                lines.append("[drvd] " + "  ".join(parts))
        return lines

    d = _drvd_rel(val)
    if not d:
        lines.append("[drvd] <not derived>")
    else:
        parts = [f"{k}=" + (_ids_str(d[k], ctx) if k in d else "?") for k in ('out', 'outer')]
        if d.get('corr'):
            parts.append("corr-apply=" + _ids_str(d['corr'], ctx))
        if 'keys' in d:
            parts.append("keys={" + ", ".join("[" + ",".join(map(str, k)) + "]" for k in d['keys']) + "}")
        if 'maxcard' in d:
            mc = d['maxcard']
            parts.append("maxcard=" + ("unbounded" if mc >= _MAXCARD_UNBOUNDED else str(mc)))
        lines.append("[drvd] " + "  ".join(parts))

    try:
        pp = val['m_pdpplan']
        if pp != 0:
            p = pp.dereference()
            lines.append(f"[plan] dist={_one_line(_os_distspec(p['m_pds']))}  "
                         f"order={_os_orderspec(p['m_pos']).strip()}  rewind={_os_rewind(p['m_prs'])}")
    except Exception:
        pass
    try:
        pr = val['m_prpp']
        if pr != 0:
            r = pr.dereference()
            parts = ["cols=" + _ids_str(_crs_ids(r['m_pcrs']), ctx)]
            for label, fld, spec, fn in (("dist", 'm_ped', 'm_pds', lambda x: _one_line(_os_distspec(x))),
                                         ("order", 'm_peo', 'm_pos', lambda x: _os_orderspec(x).strip()),
                                         ("rewind", 'm_per', 'm_prs', _os_rewind)):
                if r[fld] != 0:
                    parts.append(f"{label}={fn(r[fld].dereference()[spec])}")
            lines.append("[reqd] " + "  ".join(parts))
    except Exception:
        pass
    return lines

# ---------- 上下文：列名、产生/引用位置、检查结果 ----------

class _ExprCtx:
    def __init__(self, root, props=False, check=False, paths=False):
        self.root = root
        self.props, self.check, self.paths = props, check, paths
        self.names = {}            # colid -> name
        self.defs = {}             # colid -> [path]
        self.refs = []             # (path, t, field, kind, colid, is_def)
        self.nodes = {}            # path -> (val, t)
        self.flags = {}            # path -> [msg]
        self.notes = []
        self._scan()
        if check:
            self._check()

    def colname(self, i):
        n = self.names.get(i)
        return f'"{n}" ({i})' if n is not None else f"({i})"

    def flag(self, path, msg):
        lst = self.flags.setdefault(path, [])
        if msg not in lst:
            lst.append(msg)

    def _scan(self):
        for val, path in _walk(self.root):
            pop, t = _expr_op(val)
            if pop is None:
                continue
            self.nodes[path] = (val, t)
            for fname, kind, ids in _node_colrefs(pop):
                isdef = _is_def_field(t, fname)
                for i, name in ids:
                    if name is not None:
                        self.names.setdefault(i, name)
                    self.refs.append((path, t, fname, kind, i, isdef))
                    if isdef:
                        self.defs.setdefault(i, []).append(path)

    def _check(self):
        # (a) 引用了树里没有任何节点产生的列
        for path, t, fname, kind, i, isdef in self.refs:
            if not isdef and kind != 'set' and i not in self.defs:
                self.flag(path, f"{self.colname(i)} not produced in this tree (outer ref or dangling)")
        # (b) 用已推导属性核对：标量孩子用到的列 ⊆ 关系孩子的输出 ∪ 本节点 outer refs
        for path, (val, t) in self.nodes.items():
            if t.startswith("CScalar"):
                continue
            outs, used, ok = set(), set(), True
            for c in _expr_children(val):
                if c is None:
                    continue
                _, ct = _expr_op(c)
                if ct is None:
                    continue
                if ct.startswith("CScalar"):
                    d = _drvd_scalar(c)
                    if not d or 'used' not in d:
                        ok = False
                        break
                    used |= set(d['used'])
                else:
                    d = _drvd_rel(c)
                    if not d or 'out' not in d:
                        ok = False
                        break
                    outs |= set(d['out'])
            me = _drvd_rel(val)
            if not ok or not used or not me or 'outer' not in me:
                continue
            missing = used - outs - set(me['outer'])
            if missing:
                self.flag(path, "scalar child uses " + _ids_str(sorted(missing), self)
                          + " not output by children nor in outer refs")
        root = _drvd_rel(self.root)
        if root and root.get('outer'):
            self.notes.append(f"root has outer refs {_ids_str(root['outer'], self)}"
                              " (fine for a correlated subtree, a bug for a whole query)")

def print_cexpression_checked(root, path, props, check, paths):
    with SilenceGDBWarnings():
        ctx = _ExprCtx(root, props=props, check=check, paths=paths)
        print_cexpression(_subexpr(root, path), ctx=ctx, path=path)
        if check:
            n = sum(len(v) for v in ctx.flags.values())
            for note in ctx.notes:
                print("note: " + note)
            print(f"check: {n} problem(s)" + ("" if n == 0 else
                  " -- marked with !! above; inspect a node with `orcaprint -p <expr> @path`"))

# ---------- orcaprint colref <id|name> <expr> ----------

def print_colref_usage(root, query, sub=()):
    _subexpr(root, sub)                       # 路径不存在时报错
    with SilenceGDBWarnings():
        ctx = _ExprCtx(root)
        if re.fullmatch(r'\d+', query):
            ids = [int(query)]
        else:
            q = query.strip('"\'')
            ids = sorted(i for i, n in ctx.names.items() if n == q)
            if not ids:
                print(f"no column named '{q}' referenced in this tree")
                return
        for i in ids:
            print(f"column {ctx.colname(i)}:")
            rows = []
            for path, t, fname, kind, cid, isdef in ctx.refs:
                if cid == i and path[:len(sub)] == sub:
                    rows.append(("def" if isdef else "use", path, f"{t}.{fname}"))
            for path, (val, t) in ctx.nodes.items():
                if t.startswith("CScalar") or path[:len(sub)] != sub:
                    continue
                d = _drvd_rel(val)
                if d and i in d.get('outer', ()):
                    rows.append(("outer", path, f"{t}  (outer ref of this subtree)"))
            rows.sort(key=lambda r: r[1])
            w = max([len(_path_str(r[1])) for r in rows] + [5])
            for role, path, what in rows:
                print(f"  {role:<5} {_path_str(path):<{w}}  {what}")
            if i not in ctx.defs:
                print("  !! no node in this tree produces it"
                      " (outer ref of a subtree, or a dangling colref)")

# ---------- orcatrace：逐步 next 过一个函数，打印每一步对 CExpression 的改动 ----------

def _cexpr_locals(frame):
    """frame 里所有 CExpression* 局部变量/参数 -> 地址"""
    res = {}
    try:
        blk = frame.block()
    except RuntimeError:
        return res
    while blk is not None:
        for sym in blk:
            if not (sym.is_variable or sym.is_argument):
                continue
            try:
                ty = sym.type.strip_typedefs()
                if ty.code == gdb.TYPE_CODE_PTR and str(ty.target().strip_typedefs()) == 'gpopt::CExpression':
                    res.setdefault(sym.name, int(sym.value(frame)))
            except Exception:
                pass
        if blk.function is not None:
            break
        blk = blk.superblock
    return res

_SRC_CACHE = {}
_CALLEE_RE = re.compile(r'([A-Za-z_][\w:]*)\s*\(')
_NOT_CALLEE = {"if", "for", "while", "switch", "return", "sizeof", "GPOS_NEW", "GPOS_ASSERT",
               "GPOS_CHECK_STACK_SIZE", "GPOS_CHECK_ABORT", "PoctxtFromTLS"}

def _callee_name(symtab, first, last):
    """源码 [first, last) 行里第一个像 Pexpr* 的调用名"""
    fn = symtab.fullname()
    if fn not in _SRC_CACHE:
        try:
            _SRC_CACHE[fn] = open(fn).read().split("\n")
        except Exception:
            _SRC_CACHE[fn] = []
    src = "\n".join(_SRC_CACHE[fn][max(first - 1, 0):max(last - 1, first)])
    names = [n for n in _CALLEE_RE.findall(src) if n.split("::")[-1] not in _NOT_CALLEE]
    for n in names:
        if "Pexpr" in n:
            return n
    return names[0] if names else None

def _render(addr):
    lines = []
    try:
        val = gdb.Value(addr).cast(gdb.lookup_type("gpopt::CExpression").pointer()).dereference()
        _os_expr_lines(val, "", True, 0, lines)
    except Exception as e:
        lines.append(f"<cannot print: {e}>")
    return lines

class ORCATrace(gdb.Command):
    """orcatrace [--full] [function]
在 function（默认 gpopt::CExpressionPreprocessor::PexprPreprocess）里逐行 next，
每当一个 CExpression* 局部变量被赋值，就把新树和上一棵树 diff 并打印，最后汇总哪些步骤改了树。
当前不在该函数里时先在入口打临时断点再 continue。跟踪期间暂时禁用其它断点，结束后恢复。
  --full   每个有改动的步骤打印完整的树，而不是 diff"""

    def __init__(self):
        super().__init__("orcatrace", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        import difflib
        self.dont_repeat()
        args = arg.split()
        full = "--full" in args
        args = [a for a in args if a != "--full"]
        func = args[0] if args else "gpopt::CExpressionPreprocessor::PexprPreprocess"
        short = func.split("::")[-1]

        def frame_matches(f):
            n = f.name() if f is not None else None
            if n is None:
                return False
            return n == func or n.endswith("::" + func) or ("::" not in func and n.split("::")[-1] == func)

        try:
            frame = gdb.selected_frame()
        except gdb.error:
            raise gdb.GdbError("no process")
        if not frame_matches(frame):
            if frame_matches(gdb.newest_frame()):
                frame = gdb.newest_frame()
            else:
                f = frame.older()
                while f is not None and not frame_matches(f):
                    f = f.older()
                if f is not None:
                    raise gdb.GdbError(f"{func} is an outer frame; `finish` back to it first, "
                                       "or run orcatrace before it is called")
                print(f"orcatrace: waiting for {func} ...")
                gdb.execute(f"tbreak {func}", to_string=True)
                gdb.execute("continue")
                frame = gdb.selected_frame()
                if not frame_matches(frame):
                    raise gdb.GdbError(f"stopped elsewhere ({frame.name()}), not in {func}")

        others = [b for b in gdb.breakpoints() if b.enabled and b.is_valid()]
        for b in others:
            b.enabled = False
        steps = []
        try:
            vars_ = _cexpr_locals(frame)
            # 起点：第一个 CExpression* 参数
            cur_name, cur_addr = None, 0
            blk = frame.block()
            while blk is not None and blk.function is None:
                blk = blk.superblock
            for sym in blk or []:
                if sym.is_argument and sym.name in vars_:
                    cur_name, cur_addr = sym.name, vars_[sym.name]
                    break
            with SilenceGDBWarnings():
                cur = _render(cur_addr) if cur_addr else []
                print(f"== {frame.name()}  input {cur_name}: {len(cur)} lines")
                if full:
                    print("\n".join(cur))
            seen_addrs = {cur_addr}
            while True:
                sal = frame.find_sal()
                first = sal.line
                before = _cexpr_locals(frame)
                try:
                    gdb.execute("next", to_string=True)
                except gdb.error as e:
                    print(f"orcatrace: stopped: {e}")
                    break
                if not frame.is_valid():
                    print(f"== left {short} (returned or threw); now in {gdb.selected_frame().name()}")
                    break
                newest = gdb.newest_frame()
                if newest != frame:
                    print(f"orcatrace: stopped inside {newest.name()} (signal?); trace aborted")
                    break
                after = _cexpr_locals(frame)
                last = frame.find_sal().line
                for name, addr in after.items():
                    if addr == before.get(name) or addr == 0:
                        continue
                    callee = _callee_name(sal.symtab, first, last) if sal.symtab else None
                    if addr in seen_addrs:
                        if callee is None:        # 纯别名赋值，如 pexprConvert2In = pexprUnnested
                            continue
                        steps.append((f"{callee} -> {name}", 0, 0))
                        print(f"-- [{len(steps)}] {callee} -> {name}: unchanged (returned the same object)")
                        continue
                    seen_addrs.add(addr)
                    with SilenceGDBWarnings():
                        new = _render(addr)
                        diff = list(difflib.unified_diff(cur, new, lineterm="", n=2))[2:]
                        label = f"{callee or '?'} -> {name}"
                        if not diff:
                            steps.append((label, 0, 0))
                            print(f"-- [{len(steps)}] {label}: unchanged")
                        else:
                            add = sum(1 for d in diff if d.startswith("+"))
                            rm = sum(1 for d in diff if d.startswith("-"))
                            steps.append((label, add, rm))
                            print(f"-- [{len(steps)}] {label}: +{add} -{rm}  (line {first})")
                            print("\n".join(new if full else diff))
                    cur = new
        finally:
            for b in others:
                if b.is_valid():
                    b.enabled = True
        changed = [(k + 1, s) for k, s in enumerate(steps) if s[1] or s[2]]
        print(f"== {len(steps)} step(s), {len(changed)} changed the tree:")
        for k, (label, add, rm) in changed:
            print(f"   [{k}] {label}  +{add} -{rm}")

ORCATrace()


def print_cdparray(val, depth=0):
    """Generic pretty printer for CDynamicPtrArray<T, Cleanup*> (including nested types)."""
    pad = mkpad(depth)
    if depth > MAX_DEPTH:
        print(pad + f"  [warn] depth > {MAX_DEPTH}, stop (cycle or garbage?)")
        return
    print(pad + "=" * 60, flush=True)
    tname = str(val.type.strip_typedefs())
    print(pad + f"{tname} @ {val.address}", flush=True)

    try:
        size = array_size(val)
        capacity = int(val['m_capacity'])
        print(pad + f"  size     = {size}", flush=True)
        print(pad + f"  capacity = {capacity}", flush=True)

        elems = val['m_elems']
        if elems == 0 or size == 0:
            print(pad + "  elems: (empty)", flush=True)
            print(pad + "=" * 60, flush=True)
            return

        print(pad + "  elems:", flush=True)
        for i in range(size):
            try:
                ptr = (elems + i).dereference()
                elem = safe_deref(ptr)
                if not elem:
                    print(pad + f"    [{i}] = <NULL>", flush=True)
                    continue

                etype = str(ptr.type).replace('*', '').strip()

                # --- 基本类型 ---
                if any(t in etype for t in ['unsigned long', 'int', 'char']):
                    print(pad + f"    [{i}] = {int(elem)}", flush=True)

                # --- CWStringBase 类（打印宽字符串）---
                elif 'CWStringBase' in etype or 'CWStringConst' in etype:
                    print(pad + f"    [{i}] = \"{get_cw_string(elem)}\"", flush=True)

                elif 'CMDName' in etype:
                    print(pad + f"    [{i}] = \"{parse_cmdname(elem)}\"", flush=True)

                # --- 嵌套 CDynamicPtrArray（递归打印）---
                elif 'CDynamicPtrArray' in etype:
                    print(pad + f"    [{i}]:", flush=True)
                    print_cdparray(elem, depth + 3)

                    # --- CExpression 元素 ---
                elif 'CExpression' in etype or 'gpopt::CExpression' in etype:
                    print(pad + f"    [{i}]:", flush=True)
                    print_cexpression(elem, depth + 3)

                else:
                    print(pad + f"    [{i}] = {get_str(elem)}", flush=True)

            except Exception as e:
                print(pad + f"    [{i}] = [error: {e}]", flush=True)

    except Exception as e:
        print(pad + f"  [warn] failed to print array: {e}", flush=True)

    print(pad + "=" * 60, flush=True)



def _group_id(pgroup):
    try:
        if pgroup == 0:
            return "NULL"
        return str(int(pgroup.dereference()['m_id']))
    except Exception:
        return "?"

def _enum_str(val, name):
    try:
        return str(val[name])
    except Exception:
        return "?"

def print_cgroupexpression(val, depth=0):
    pad = mkpad(depth)
    pop = val['m_pop']
    op_name = _op_dynamic_type(pop)
    print(pad + "=" * 60, flush=True)
    print(pad + f"CGroupExpression @ {val.address}", flush=True)
    print(pad + f"  id       = {int(val['m_id'])}", flush=True)
    print(pad + f"  operator = {op_name}", flush=True)
    print(pad + f"  group    = {_group_id(val['m_pgroup'])}", flush=True)

    child_ids = []
    arr_ptr = val['m_pdrgpgroup']
    if arr_ptr != 0:
        arr = arr_ptr.dereference()
        elems = arr['m_elems']
        child_ids = [_group_id((elems + i).dereference()) for i in range(array_size(arr))]
    print(pad + f"  children = [{', '.join(child_ids)}]", flush=True)

    print(pad + f"  state    = {_enum_str(val, 'm_estate')}", flush=True)
    print(pad + f"  opt level= {_enum_str(val, 'm_eol')}", flush=True)
    print(pad + f"  xform    = {_enum_str(val, 'm_exfidOrigin')}", flush=True)
    if _has_field(val, 'm_pgexprOrigin') and val['m_pgexprOrigin'] != 0:
        origin = val['m_pgexprOrigin'].dereference()
        print(pad + f"  origin   = gexpr {int(origin['m_id'])} (group {_group_id(origin['m_pgroup'])})", flush=True)
    if val['m_pgexprDuplicate'] != 0:
        dup = val['m_pgexprDuplicate'].dereference()
        print(pad + f"  duplicate= gexpr {int(dup['m_id'])}", flush=True)
    print(pad + f"  circular = {_enum_str(val, 'm_ecirculardependency')}", flush=True)
    print(pad + "=" * 60, flush=True)

# --------- required plan properties (CReqdPropPlan & friends) ----------
def _dyn_deref(ptr):
    """指针 -> 按动态类型转换后的对象(RTTI 找不到时退回静态类型)；NULL 返回 None。"""
    try:
        if ptr == 0:
            return None
        try:
            return ptr.cast(ptr.dynamic_type).dereference()
        except Exception:
            return ptr.dereference()
    except Exception:
        return None

def _list_elems(lst):
    """遍历 gpos::CList<T>：元素内嵌 SLink，m_head 之后沿 m_next 走。返回元素指针列表。"""
    res = []
    offset = int(lst['m_offset'])
    n = int(lst['m_size'])
    if n < 0 or n > MAX_ARRAY_ELEMS:
        raise gdb.GdbError(f"bogus list m_size={n:#x}")
    vpp = gdb.lookup_type('void').pointer().pointer()
    elem = lst['m_head']
    for _ in range(n):
        if elem == 0:
            break
        res.append(elem)
        raw = elem.cast(gdb.lookup_type('char').pointer()) + offset
        elem = raw.cast(vpp).dereference().cast(elem.type)  # SLink::m_next 在偏移 0
    return res

def bitset_members(bs):
    """CBitSet / CColRefSet 中置位的 bit 下标列表。"""
    bits = []
    for link_ptr in _list_elems(bs['m_bsllist']):
        link = link_ptr.dereference()
        offset = int(link['m_offset'])
        vec = link['m_vec'].dereference()
        nbits = int(vec['m_nbits'])
        words = vec['m_vec']
        for i in range(int(vec['m_len'])):
            w = int((words + i).dereference()) & 0xFFFFFFFFFFFFFFFF
            b = 0
            while w:
                if w & 1:
                    pos = i * 64 + b
                    if pos < nbits:
                        bits.append(offset + pos)
                w >>= 1
                b += 1
    return sorted(bits)

def _hashmap_items(hm):
    """遍历 gpos::CHashMap：返回 [(key_ptr, value_ptr), ...]。"""
    items = []
    nchains = int(hm['m_num_chains'])
    if nchains < 0 or nchains > MAX_ARRAY_ELEMS:
        raise gdb.GdbError(f"bogus hashmap m_num_chains={nchains:#x}")
    chains = hm['m_chains']
    for c in range(nchains):
        chain_ptr = (chains + c).dereference()
        if chain_ptr == 0:
            continue
        chain = chain_ptr.dereference()
        elems = chain['m_elems']
        for i in range(array_size(chain)):
            e = (elems + i).dereference().dereference()
            items.append((e['m_key'], e['m_value']))
    return items

def _colref_str(pcr):
    try:
        if pcr == 0:
            return "NULL"
        cr = pcr.dereference()
        return f"{get_cw_string(cr['m_pname']['m_str_name'])}(id:{int(cr['m_id'])})"
    except Exception:
        return "?"

def _colrefset_str(pcrs):
    try:
        if pcrs == 0:
            return "NULL"
        ids = bitset_members(pcrs.dereference())
        return "{" + ", ".join(str(i) for i in ids) + "}"
    except Exception as e:
        return f"<error: {e}>"

def _mdid_str(pmdid):
    return f"mdid:{get_oid_from_mdid(pmdid)}"

def _scalar_fields(obj, pad):
    """打印对象(含基类)里的标量成员：bool/int/enum 类型的 m_xxx。"""
    typ = obj.type.strip_typedefs()
    for f in typ.fields():
        if f.is_base_class:
            _scalar_fields(obj[f], pad)
            continue
        if not f.name or not f.name.startswith('m_'):
            continue
        ftype = f.type.strip_typedefs()
        if ftype.code in (gdb.TYPE_CODE_BOOL, gdb.TYPE_CODE_INT, gdb.TYPE_CODE_ENUM, gdb.TYPE_CODE_FLT):
            print(pad + f"{f.name} = {obj[f.name]}", flush=True)

def _print_exprarray(arr_ptr, depth):
    if arr_ptr == 0:
        print(mkpad(depth) + "  (NULL)", flush=True)
        return
    arr = arr_ptr.dereference()
    elems = arr['m_elems']
    for i in range(array_size(arr)):
        e = safe_deref((elems + i).dereference())
        if e is not None:
            print_cexpression(e, depth)

def print_orderspec(val, depth=0):
    pad = mkpad(depth)
    arr_ptr = val['m_pdrgpoe']
    if arr_ptr == 0:
        print(pad + "COrderSpec: (empty)", flush=True)
        return
    arr = arr_ptr.dereference()
    n = array_size(arr)
    print(pad + f"COrderSpec: {n} column(s)", flush=True)
    elems = arr['m_elems']
    for i in range(n):
        oe = safe_deref((elems + i).dereference())
        if oe is None:
            print(pad + f"  [{i}] <NULL>", flush=True)
            continue
        print(pad + f"  [{i}] {_colref_str(oe['m_pcr'])}  sortop {_mdid_str(oe['m_mdid'])}  "
                    f"nulls: {oe['m_ent']}", flush=True)

def print_distspec(val, depth=0):
    pad = mkpad(depth)
    obj = _dyn_deref(val.address) or val
    tname = shorten_type_name(obj.type)
    print(pad + f"{tname}", flush=True)
    _scalar_fields(obj, pad + "  ")
    for fname in ('m_pcrSegmentId', 'm_gp_segment_id'):
        if _has_field(obj, fname):
            print(pad + f"  {fname} = {_colref_str(obj[fname])}", flush=True)
    if _has_field(obj, 'm_pdrgpexpr'):
        print(pad + "  hash exprs:", flush=True)
        _print_exprarray(obj['m_pdrgpexpr'], depth + 2)
        if _has_field(obj, 'm_pdshashedEquiv') and obj['m_pdshashedEquiv'] != 0:
            print(pad + "  equivalent hashed spec:", flush=True)
            print_distspec(obj['m_pdshashedEquiv'].dereference(), depth + 2)

def print_rewindspec(val, depth=0):
    pad = mkpad(depth)
    print(pad + "CRewindabilitySpec", flush=True)
    _scalar_fields(val, pad + "  ")

def print_partpropspec(val, depth=0):
    pad = mkpad(depth)
    print(pad + "CPartitionPropagationSpec", flush=True)
    if val['m_scan_ids'] != 0:
        print(pad + f"  scan_ids = {_colrefset_str(val['m_scan_ids'])}", flush=True)
    if val['m_part_prop_spec_infos'] == 0:
        return
    for k, v in _hashmap_items(val['m_part_prop_spec_infos'].dereference()):
        info = v.dereference()
        sel = _colrefset_str(info['m_selector_ids'])
        print(pad + f"  scan_id {int(info['m_scan_id'])}: {info['m_type']}  "
                    f"root_rel {_mdid_str(info['m_root_rel_mdid'])}  selector_ids {sel}", flush=True)
        if info['m_filter_expr'] != 0:
            print(pad + "    filter:", flush=True)
            print_cexpression(info['m_filter_expr'].dereference(), depth + 3)

def print_cteReq(val, depth=0):
    pad = mkpad(depth)
    print(pad + "CCTEReq", flush=True)
    if val['m_phmcter'] != 0:
        for k, v in _hashmap_items(val['m_phmcter'].dereference()):
            e = v.dereference()
            print(pad + f"  cte {int(e['m_id'])}: {e['m_ect']}  required={e['m_fRequired']}", flush=True)
    if val['m_pdrgpulRequired'] != 0:
        ids = get_dynamic_ptr_array_list(val['m_pdrgpulRequired'])
        print(pad + f"  required ids = {ids}", flush=True)

def _print_enfd(label, val, spec_field, match_field, printer, depth):
    pad = mkpad(depth)
    print(pad + f"{label}  ({val[match_field]})", flush=True)
    spec = _dyn_deref(val[spec_field])
    if spec is None:
        print(pad + "  (NULL)", flush=True)
    else:
        printer(spec, depth + 1)

def print_reqdpropplan(val, depth=0):
    pad = mkpad(depth)
    print(pad + "=" * 60, flush=True)
    print(pad + f"CReqdPropPlan @ {val.address}", flush=True)
    print(pad + f"  columns: {_colrefset_str(val['m_pcrs'])}", flush=True)
    for label, fld, spec, match, printer in (
            ("order", 'm_peo', 'm_pos', 'm_eom', print_orderspec),
            ("distribution", 'm_ped', 'm_pds', 'm_edm', print_distspec),
            ("rewindability", 'm_per', 'm_prs', 'm_erm', print_rewindspec),
            ("partition propagation", 'm_pepp', 'm_ppps', 'm_eppm', print_partpropspec)):
        enfd = safe_deref(val[fld])
        if enfd is None:
            print(pad + f"  {label}: (NULL)", flush=True)
        else:
            _print_enfd("  " + label + ":", enfd, spec, match, printer, depth + 1)
    cte = safe_deref(val['m_pcter'])
    if cte is None:
        print(pad + "  cte: (NULL)", flush=True)
    else:
        print_cteReq(cte, depth + 1)
    print(pad + "=" * 60, flush=True)

def print_reqdproprelational(val, depth=0):
    pad = mkpad(depth)
    print(pad + f"CReqdPropRelational @ {val.address}", flush=True)
    print(pad + f"  stat columns: {_colrefset_str(val['m_pcrsStat'])}", flush=True)

# 安全求值器支持的语法(其余一律交还 gdb 原生解析器):
#   expr    := ('*' | '&') expr | postfix
#   postfix := primary ( '->' NAME [args] | '.' NAME [args] | '[' INT ']' )*
#   primary := NAME | '(' expr ')'
#   args    := '(' [INT (',' INT)*] ')'      INT 含 0x.. / 负数 / true / false
_TOKEN = re.compile(r'\s*(->|0[xX][0-9a-fA-F]+|\d+|[A-Za-z_]\w*|[-*&().\[\],])')

def _tokenize(s):
    toks, pos, s = [], 0, s.rstrip()
    while pos < len(s):
        m = _TOKEN.match(s, pos)
        if not m:
            return None
        toks.append(m.group(1))
        pos = m.end()
    return toks

def _find_field(typ, want):
    """在类型及其基类中找名字(忽略大小写)为 want 的字段，返回真实字段名。"""
    typ = typ.strip_typedefs()
    if typ.code == gdb.TYPE_CODE_PTR:
        typ = typ.target().strip_typedefs()
    for f in typ.fields():
        if f.is_base_class:
            r = _find_field(f.type, want)
            if r:
                return r
        elif f.name and f.name.lower() == want:
            return f.name
    return None

def _lookup_var(name):
    """只遍历当前帧的 block 链和当前文件的 static/global block 找变量。

    不能用 Frame.read_var(str) / lookup_global_symbol：它们查不到名字时会走
    C++ using-directive 的递归查找(cp_lookup_symbol_via_imports)，写错变量名
    就会让 gdb 卡死。这里只做有限的 block 遍历，找不到直接返回 None。"""
    frame = gdb.selected_frame()
    try:
        block = frame.block()
    except RuntimeError:
        block = None
    while block is not None:
        if not block.is_global and not block.is_static:
            for sym in block:
                if sym.name == name and (sym.is_variable or sym.is_argument):
                    return sym.value(frame)
        block = block.superblock
    symtab = frame.find_sal().symtab
    if symtab is not None:
        for blk in (symtab.static_block(), symtab.global_block()):
            for sym in blk:
                if sym.name == name and sym.is_variable:
                    return sym.value()
    return None

def _lookup_global_var(name):
    """按名字查全局/文件静态变量(如 optimizer、ActivePortal)。

    gdb.lookup_global_symbol/lookup_static_symbol 走 lookup_global_or_static_symbol，
    只查 objfile 的全局/静态 block，不经过 cp_lookup_symbol_via_imports，
    查不到会很快返回 None。"""
    for fn in (gdb.lookup_global_symbol, gdb.lookup_static_symbol):
        try:
            sym = fn(name)
        except gdb.error:
            sym = None
        # 头文件里的 extern 声明是 LOC_UNRESOLVED(is_variable 为 False)，
        # value() 会经 minimal symbol 解析出真实地址
        if sym is not None and (sym.is_variable or sym.addr_class == gdb.SYMBOL_LOC_UNRESOLVED):
            try:
                return sym.value()
            except gdb.error:
                pass
    return None

def _class_chain(typ):
    """typ 及其所有基类(广度优先)，元素是去掉 typedef 的 struct/class 类型。"""
    out, todo, seen = [], [typ], set()
    while todo:
        t = todo.pop(0).strip_typedefs()
        if t.code != gdb.TYPE_CODE_STRUCT or t.name is None or t.name in seen:
            continue
        seen.add(t.name)
        out.append(t)
        for f in t.fields():
            if f.is_base_class:
                todo.append(f.type)
    return out

def _find_method(obj, name):
    """找 obj(结构体值) 上名为 name 的成员函数，返回 (函数符号, 所属类)。

    用完整限定名 "ns::Class::name" 直接查全局符号，而不是让 gdb 解析
    `x->name()`：后者会触发 cp_lookup_symbol_via_imports 指数级递归。
    先查动态类型(RTTI)以保证虚函数调用到正确的 override，再查静态类型。"""
    types = []
    try:
        types.append(obj.dynamic_type)
    except gdb.error:
        pass
    types.append(obj.type)
    for start in types:
        for cls in _class_chain(start):
            try:
                sym = gdb.lookup_global_symbol(f"{cls.name}::{name}")
            except gdb.error:
                sym = None
            if sym is not None and sym.type is not None and \
               sym.type.code in (gdb.TYPE_CODE_METHOD, gdb.TYPE_CODE_FUNC):
                return sym, cls
    return None, None

def _call_method(val, name, args):
    """对 val(对象或对象指针) 调用成员函数 name(*args)，找不到函数返回 None。"""
    obj = val.dereference() if val.type.strip_typedefs().code == gdb.TYPE_CODE_PTR else val
    sym, cls = _find_method(obj, name)
    if sym is None:
        return None
    this = obj.address.cast(cls.pointer())
    return sym.value()(this, *args)

class _NotSimple(Exception):
    pass

class _SafeEval:
    """按上面的语法递归下降求值，所有名字查找/成员访问都走 Python API。"""

    def __init__(self, toks, call_methods, dry=False):
        # dry=True 只检查语法不求值：先确认整句都在支持范围内，
        # 避免像 (CExpression *)x 这样的强转被当成“变量不存在”报错
        self.toks, self.i, self.call_methods, self.dry = toks, 0, call_methods, dry

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def take(self, want=None):
        t = self.peek()
        if t is None or (want is not None and t != want):
            raise _NotSimple()
        self.i += 1
        return t

    def name(self):
        t = self.take()
        if not re.match(r'[A-Za-z_]\w*$', t):
            raise _NotSimple()
        return t

    def int_lit(self):
        neg = self.peek() == '-'
        if neg:
            self.take()
        t = self.take()
        if t in ('true', 'false') and not neg:
            return 1 if t == 'true' else 0
        if not re.match(r'(0[xX][0-9a-fA-F]+|\d+)$', t):
            raise _NotSimple()
        return -int(t, 0) if neg else int(t, 0)

    def run(self):
        v = self.expr()
        if self.peek() is not None:
            raise _NotSimple()
        return v

    def expr(self):
        t = self.peek()
        if t in ('*', '&'):
            self.take()
            v = self.expr()
            if self.dry:
                return None
            return v.dereference() if t == '*' else v.address
        return self.postfix()

    def primary(self):
        if self.peek() == '(':
            self.take()
            v = self.expr()
            self.take(')')
            return v
        base = self.name()
        if self.dry:
            return None
        val = _lookup_var(base)
        if val is None and self.call_methods:
            val = _lookup_global_var(base)
        if val is None:
            raise gdb.GdbError(f"No symbol \"{base}\" in current context.")
        return val

    def postfix(self):
        val = self.primary()
        while True:
            t = self.peek()
            if t == '[':
                self.take()
                idx = self.int_lit()
                self.take(']')
                if self.dry:
                    continue
                if val.type.strip_typedefs().code == gdb.TYPE_CODE_STRUCT:
                    # 对象上的 [n] 是 operator[]，如 CExpression 取第 n 个孩子
                    res = _call_method(val, "operator[]", [idx])
                    if res is None:
                        raise gdb.GdbError(f"cannot apply [] to {val.type}: no operator[] symbol")
                    val = res
                else:
                    val = val[idx]
            elif t in ('->', '.'):
                self.take()
                name = self.name()
                if self.peek() == '(':
                    val = self.method(val, name)
                elif not self.dry:
                    val = val[name]
            else:
                return val

    def method(self, val, name):
        self.take('(')
        args = []
        if self.peek() != ')':
            args.append(self.int_lit())
            while self.peek() == ',':
                self.take()
                args.append(self.int_lit())
        self.take(')')
        if self.dry:
            return None
        if self.call_methods:
            res = _call_method(val, name, args)
            if res is not None:
                return res
        # 访问器 Pfoo() 映射到成员 m_pfoo(ORCA 命名惯例)
        field = _find_field(val.type, "m_" + name.lower()) if not args else None
        if field is None:
            raise gdb.GdbError(f"cannot call {name}() on {val.type}: no such method "
                               f"symbol (inlined?) and no m_{name.lower()} member")
        return val[field]

def eval_simple(arg, call_methods=False):
    """不经过 gdb 的 C++ 表达式解析器求值 (*a)[1]->b.c[2]->Pfoo() 这类表达式。

    在 ORCA 代码里 gdb 解析 `x->name` 时会对 name 做符号查找(判断是否类型名)，
    遇到大量 `using namespace gpos/gpopt/...` 时 cp_lookup_symbol_via_imports
    递归穷举，CPU 100% 且 Ctrl-C/SIGTERM 都无效；连 `p pqc->m_pexpr` 都会卡死。
    这里变量名只做有限的 block 查找，字段用 Python API 访问。
    方法调用：call_methods=True 时按限定名找到成员函数做真正的 inferior call
    (见 _find_method)；否则或函数不存在(被内联掉)时映射到 m_xxx 成员。
    对象上的 [n] 调用 operator[]。
    返回 None 表示超出支持的语法，由调用者回退到 gdb 原生解析。"""
    toks = _tokenize(arg)
    if not toks:
        return None
    try:
        _SafeEval(toks, call_methods, dry=True).run()
    except _NotSimple:
        return None
    return _SafeEval(toks, call_methods).run()

class SafePrintParam(gdb.Parameter):
    """Evaluate simple C++ member chains in `print` without gdb's expression parser."""
    set_doc = "Set whether print/p evaluates simple C++ chains via Python."
    show_doc = "Show whether print/p evaluates simple C++ chains via Python."

    def __init__(self):
        super().__init__("orca-safe-print", gdb.COMMAND_DATA, gdb.PARAM_BOOLEAN)
        self.value = True

    def get_show_string(self, svalue):
        return f"orca-safe-print is {svalue}."

_safe_print_param = SafePrintParam()

_PRINT_FMT = re.compile(r'\s*(/\w+)?\s*(.*)$', re.S)

def _in_cxx_frame():
    try:
        return gdb.selected_frame().language() == 'c++'
    except (gdb.error, AttributeError):
        return False

class SafePrint(gdb.Command):
    """Print value of expression EXP (C++-safe wrapper around gdb's print).

Usage: print[/FMT] [EXP]   (also: p, inspect)

In C++ frames, simple chains such as
    pexprInner->Arity()
    pexpr->Pop()->Eopid()
    (*pexpr)[1]->Arity()          (operator[] is called too)
    *pexpr->m_pdrgpexpr
    ActivePortal->name            (globals work)
are evaluated through gdb's Python API: the first name is looked up in
the current frame/globals, fields are read directly, and methods are
called by looking up "Class::Method" as a global symbol (dynamic type
first, so virtual calls dispatch correctly).

Reason: for `x->Name`, gdb 15's C++ parser looks up Name through every
`using namespace` directive (cp_lookup_symbol_via_imports). In ORCA,
with hundreds of such directives, that search is exponential: gdb spins
at 100% CPU and ignores Ctrl-C.

Anything else (casts, arithmetic, print options, $vars, ...) is passed
to gdb's own evaluator unchanged.  Disable with: set orca-safe-print off"""

    def __init__(self):
        super().__init__("print", gdb.COMMAND_DATA, gdb.COMPLETE_EXPRESSION)

    def _builtin(self, arg):
        # 原生 print 已被本命令覆盖；call 与 print 的解析/格式/选项完全一致，
        # 唯一区别是 void 结果不打印
        if not arg.strip():
            arg = "$"  # 与无参数 print 一致：显示上一个历史值
        gdb.execute("call " + arg, from_tty=True)

    def invoke(self, arg, from_tty):
        if not _safe_print_param.value or arg.lstrip().startswith('-') or not _in_cxx_frame():
            return self._builtin(arg)
        m = _PRINT_FMT.match(arg)
        fmt, expr = m.group(1) or "", m.group(2)
        try:
            val = eval_simple(expr, call_methods=True) if expr else None
            if val is None:
                return self._builtin(arg)
            if val.type.strip_typedefs().code == gdb.TYPE_CODE_VOID:
                gdb.write("$%d = void\n" % gdb.add_history(val))
                return
            val.fetch_lazy()  # 在这里触发 NULL/野指针的读内存错误
            idx = gdb.add_history(val)
        except gdb.GdbError:
            raise
        except gdb.error as e:  # 含 gdb.MemoryError：按 gdb 原生错误输出，不打 Python 堆栈
            raise gdb.GdbError(str(e))
        # 用 output $N 打印：沿用 gdb 自己的格式化(/x、pretty printer、set print ...)
        gdb.write(f"${idx} = ")
        gdb.execute(f"output{fmt} ${idx}")
        gdb.write("\n")

SafePrint()

class ORCAPrint(gdb.Command):
    """ORCAPrint <var> -- Pretty print gpdxl structures (CDXLTableDescr / CDXLNode)"""

    def __init__(self):
        super(ORCAPrint, self).__init__("orcaprint", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        # 空回车不重复执行：卡住时连按回车会排队 N 次同样的打印
        self.dont_repeat()
        usage = ("Usage: orcaprint [-p] [--check] [-n] <expr> [@path]\n"
                 "       orcaprint colref <colid|name> <expr> [@path]\n"
                 "  -p       show derived props ([drvd] out/outer/keys/maxcard, used/defined;\n"
                 "           physical: [plan] derived and [reqd] required dist/order/rewind)\n"
                 "  --check  mark nodes referencing columns nobody produces (!!)\n"
                 "  -n       append each node's @path\n"
                 "  @path    print only that subtree, e.g. @1.0 = child 1 of root, then its child 0")
        toks = arg.split()
        colref_q = None
        if len(toks) >= 3 and toks[0] == "colref":
            colref_q = toks[1]
            toks = toks[2:]
        opts = {t for t in toks if t in ("-p", "--check", "-n")}
        toks = [t for t in toks if t not in opts]
        path = ()
        if toks and toks[-1].startswith("@"):
            path = _parse_path(toks.pop())
        arg = " ".join(toks)
        if not arg:
            print(usage)
            return

        val = eval_simple(arg)
        if val is None:
            val = gdb.parse_and_eval(arg)
        if val.type.strip_typedefs().code == gdb.TYPE_CODE_PTR:
            val = safe_deref(val)
        if val is None:
            print(f"{arg} is NULL or invalid")
            return

        tname = str(val.type.strip_typedefs())

        if tname == 'gpopt::CQueryContext' and (colref_q is not None or opts or path):
            val = safe_deref(val['m_pexpr'])        # 这些选项作用在 m_pexpr 上
            if val is None:
                print("m_pexpr is NULL")
                return
            tname = 'gpopt::CExpression'
        if tname == 'gpopt::CExpression':
            if colref_q is not None:
                print_colref_usage(val, colref_q, path)
                return
            if opts or path:
                print_cexpression_checked(val, path, "-p" in opts, "--check" in opts, "-n" in opts)
                return
        elif colref_q is not None or opts or path:
            print(f"orcaprint: colref/-p/--check/-n/@path only apply to CExpression, got '{tname}'")
            return

        # eliminate the warning message of "warning: RTTI symbol not found for class"
        with SilenceGDBWarnings():
            if 'CDXLTableDescr' in tname:
                print_cdxltabledescr(val)
            elif 'CDXLNode' in tname:
                print_cdxlnode(val)
            elif 'CDynamicPtrArray' in tname:
                print_cdparray(val)
            elif 'CReqdPropPlan' in tname:
                print_reqdpropplan(val)
            elif 'CReqdPropRelational' in tname:
                print_reqdproprelational(val)
            elif 'CEnfdOrder' in tname:
                _print_enfd("CEnfdOrder", val, 'm_pos', 'm_eom', print_orderspec, 0)
            elif 'CEnfdDistribution' in tname:
                _print_enfd("CEnfdDistribution", val, 'm_pds', 'm_edm', print_distspec, 0)
            elif 'CEnfdRewindability' in tname:
                _print_enfd("CEnfdRewindability", val, 'm_prs', 'm_erm', print_rewindspec, 0)
            elif 'CEnfdPartitionPropagation' in tname:
                _print_enfd("CEnfdPartitionPropagation", val, 'm_ppps', 'm_eppm', print_partpropspec, 0)
            elif 'COrderSpec' in tname:
                print_orderspec(val)
            elif 'CDistributionSpec' in tname:
                print_distspec(val)
            elif 'CRewindabilitySpec' in tname:
                print_rewindspec(val)
            elif 'CPartitionPropagationSpec' in tname:
                print_partpropspec(val)
            elif 'CCTEReq' in tname:
                print_cteReq(val)
            elif 'CColRefSet' in tname or 'CBitSet' in tname:
                print(_colrefset_str(val.address))
            elif tname == 'gpopt::CQueryContext':
                print_cquerycontext(val)
            elif 'CGroupExpression' in tname:
                print_cgroupexpression(val)
            elif 'CExpression' in tname or 'gpopt::CExpression' in tname:
                print_cexpression(val)
            else:
                print(f"orcaprint: unsupported type '{tname}'")

ORCAPrint()



