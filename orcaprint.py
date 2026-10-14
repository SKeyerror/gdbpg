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
    try:
        val = gdb.selected_frame().read_var(base)
    except (ValueError, gdb.error):
        sym = gdb.lookup_global_symbol(base) or gdb.lookup_static_symbol(base)
        if sym is None:
            raise gdb.GdbError(f"orcaprint: no variable '{base}' in current frame")
        val = sym.value()
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
            elif 'CExpression' in tname or 'gpopt::CExpression' in tname:
                print_cexpression(val)
            else:
                print(f"orcaprint: unsupported type '{tname}'")

ORCAPrint()



