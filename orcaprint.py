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
# def print_cexpression(val, depth=0, max_depth=8):
#     pad = mkpad(depth)
#     # print(pad + "-" * 60)
#     tname = str(val.type.strip_typedefs())
#
#     # print(pad + f"{tname} @ {val.address}")
#     # operator
#     pop = val['m_pop'] if _has_field(val, 'm_pop') else 0
#     # print(pad + f"  m_pop (operator): {get_str(pop)}")
#
#     dynamic_type = _op_dynamic_type(pop)
#     if "CLogicalGet" in dynamic_type:
#         m_op_dynamic = pop.cast(gdb.lookup_type("gpopt::CLogicalGet").pointer())
#         print(pad + f"operator:  {_op_dynamic_type(pop)}, m_pnameAlias:  {get_cw_string(m_op_dynamic["m_pnameAlias"]["m_str_name"])}", flush=True)
#     elif "CScalarAggFunc" in dynamic_type:
#         m_op_dynamic = pop.cast(gdb.lookup_type("gpopt::CScalarAggFunc").pointer())
#         print(pad + f"operator:  {_op_dynamic_type(pop)}, m_pstrAggFunc:  {get_cw_string(m_op_dynamic["m_pstrAggFunc"])}", flush=True)
#     elif "CScalarConst" in dynamic_type:
#         const_val = get_scalar_const_value_str(pop)
#         print(pad + f"operator:  {dynamic_type} (Value: {const_val})", flush=True)
#     else:
#         print(pad + f"operator:  {_op_dynamic_type(pop)}", flush=True)
#
#     # children
#     if _has_field(val, 'm_pdrgpexpr'):
#         arr = val['m_pdrgpexpr']
#         if arr == 0:
#             pass
#         else:
#             try:
#                 n = int(arr['m_size'])
#                 if n == 0:
#                     return
#                 print(pad + f"  children[{n}]:", flush=True)
#
#                 elems = arr['m_elems']
#                 for i in range(n):
#                     try:
#                         child_ptr = (elems + i).dereference()
#                         child = safe_deref(child_ptr)
#                         if child:
#                             print(pad + f"    [{i}]:", flush=True)
#                             print_cexpression(child, depth + 2, max_depth)
#                         else:
#                             print(pad + f"    [{i}] = <NULL>", flush=True)
#                     except Exception as e:
#                         print(pad + f"    [{i}] = [error: {e}]", flush=True)
#             except Exception as e:
#                 print(pad + f"  [warn] cannot expand children: {e}", flush=True)
def print_cexpression(val, depth=0, suffix=""):
    pad = mkpad(depth)
    if depth > MAX_DEPTH:
        print(pad + f"  [warn] depth > {MAX_DEPTH}, stop (cycle or garbage?)")
        return
    pop = val['m_pop'] if _has_field(val, 'm_pop') else 0
    if pop == 0:
        return

    dynamic_type = _op_dynamic_type(pop)
    clean_type = dynamic_type.split("::")[-1]

    header = pad + f"  ▶ {clean_type}"
    if suffix:
        header += f" ({suffix})"

    # --- 针对性解析逻辑 ---

    # 1. 处理投影元素 (CScalarProjectElement)
    if "CScalarProjectElement" in clean_type:
        m_op_dynamic = pop.cast(gdb.lookup_type("gpopt::CScalarProjectElement").pointer())
        pcr_ptr = m_op_dynamic['m_pcr']
        if pcr_ptr != 0:
            pcr = pcr_ptr.dereference()
            # 这里的 m_pname 是 CWStringConst
            col_name = get_cw_string(pcr['m_pname']['m_str_name'])
            print(header + f"  Defined Col: {col_name} (id: {pcr['m_id']})")
        else:
            print(header)

    # 2. 处理比较运算 (CScalarCmp)
    elif "CScalarCmp" in clean_type:
        m_op_dynamic = pop.cast(gdb.lookup_type("gpopt::CScalarCmp").pointer())
        op_name = get_cw_string(m_op_dynamic['m_pstrOp'])
        # 也可以从 m_mdid_op 获取 OID，如果需要的话
        print(header + f"  Op: '{op_name}'")

    # 3. 处理逻辑查询 (CLogicalGet)
    elif "CLogicalGet" in clean_type:
        m_op_dynamic = pop.cast(gdb.lookup_type("gpopt::CLogicalGet").pointer())
        alias = get_cw_string(m_op_dynamic["m_pnameAlias"]["m_str_name"])
        print(header + f"  Alias: {alias}")

    # 4. 处理标量标识符 (CScalarIdent)
    elif "CScalarIdent" in clean_type:
        m_op_dynamic = pop.cast(gdb.lookup_type("gpopt::CScalarIdent").pointer())
        pcr = m_op_dynamic['m_pcr'].dereference()
        col_name = get_cw_string(pcr['m_pname']['m_str_name'])
        print(header + f"  {col_name} colid: {pcr['m_id']}")

    else:
        print(header)

    # --- 递归处理子节点 ---
    if _has_field(val, 'm_pdrgpexpr') and val['m_pdrgpexpr'] != 0:
        try:
            arr = val['m_pdrgpexpr'].dereference()
            num = array_size(arr)
            elems = arr['m_elems']

            for i in range(num):
                child_ptr = (elems + i).dereference()
                child = safe_deref(child_ptr)
                if child:
                    # 智能后缀推断
                    new_suffix = ""
                    if "CLogicalSelect" in clean_type:
                        new_suffix = "data" if i == 0 else "condition"
                    elif "CLogicalProject" in clean_type:
                        new_suffix = "data" if i == 0 else "proj_list"
                    elif "CScalarSubqueryExists" in clean_type:
                        new_suffix = "subquery"
                    elif "CScalarCmp" in clean_type:
                        new_suffix = "left" if i == 0 else "right"

                    print_cexpression(child, depth + 2, new_suffix)
        except Exception as e:
            print(pad + f"  [warn] child access error: {e}")

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

_SIMPLE_EXPR = re.compile(r'\s*\*?\s*([A-Za-z_]\w*)((?:\s*(?:->|\.)\s*[A-Za-z_]\w*(?:\s*\(\s*\))?|\s*\[\s*\d+\s*\])*)\s*$')
_SIMPLE_STEP = re.compile(r'\s*(?:(->|\.)\s*([A-Za-z_]\w*)(\s*\(\s*\))?|\[\s*(\d+)\s*\])')

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

def eval_simple(arg):
    """不经过 gdb 的 C++ 表达式解析器求值 a->b.c[2]->Pfoo() 这类简单链。

    在 ORCA 代码里 gdb 解析 `x->name` 时会对 name 做符号查找(判断是否类型名)，
    遇到大量 `using namespace gpos/gpopt/...` 时 cp_lookup_symbol_via_imports
    递归穷举，CPU 100% 且 Ctrl-C/SIGTERM 都无效；连 `p pqc->m_pexpr` 都会卡死。
    这里只对首个变量名做局部查找，字段用 Python API 访问；访问器方法 Pfoo()
    映射到同名成员 m_pfoo(ORCA 命名惯例)，不做 inferior call。
    返回 None 表示不是简单链，由调用者回退到 parse_and_eval。"""
    m = _SIMPLE_EXPR.match(arg)
    if not m:
        return None
    base, rest = m.group(1), m.group(2)
    val = _lookup_var(base)
    if val is None:
        raise gdb.GdbError(f"orcaprint: no variable '{base}' in current frame")
    for st in _SIMPLE_STEP.finditer(rest):
        _, name, call, idx = st.groups()
        if idx is not None:
            val = val[int(idx)]
            continue
        field = name
        if call:
            field = _find_field(val.type, "m_" + name.lower())
            if field is None:
                raise gdb.GdbError(f"orcaprint: cannot map method {name}() to a member "
                                   f"of {val.type}; use the m_xxx field name instead")
        val = val[field]
    return val

class ORCAPrint(gdb.Command):
    """ORCAPrint <var> -- Pretty print gpdxl structures (CDXLTableDescr / CDXLNode)"""

    def __init__(self):
        super(ORCAPrint, self).__init__("orcaprint", gdb.COMMAND_USER)

    def invoke(self, arg, from_tty):
        # 空回车不重复执行：卡住时连按回车会排队 N 次同样的打印
        self.dont_repeat()
        if not arg:
            print("Usage: orcaprint <variable>")
            return

        val = eval_simple(arg)
        if val is None:
            val = gdb.parse_and_eval(arg)
        val = safe_deref(val)
        if val is None:
            print(f"{arg} is NULL or invalid")
            return

        tname = str(val.type.strip_typedefs())

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
            elif 'CGroupExpression' in tname:
                print_cgroupexpression(val)
            elif 'CExpression' in tname or 'gpopt::CExpression' in tname:
                print_cexpression(val)
            else:
                print(f"orcaprint: unsupported type '{tname}'")

ORCAPrint()



