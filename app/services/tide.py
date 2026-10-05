"""潮位修订的差量链式存储与还原。

存储模型
--------
- ``tide_revisions.base_revision``：本修订的**基础修订**号。``NULL`` 表示
  **物化修订**——全部逐分钟数据直接保存在 ``tide_values``（旧库既有修订与
  初始/演示修订均属此类）；非空表示**差量修订**——只把相对基础修订发生
  变化的分钟写入 ``tide_deltas``，未改分钟沿基础修订链逐层继承。
- ``tide_deltas(revision, minute, level, error)``：差量行保存该分钟在
  **本修订下的最终值**（名义潮位与误差），而非与基础的差值——还原时沿链
  取"最近一层"的一行即可，无需逐层求差。

因此每次修订的持久化写入量只随**本次改动分钟数**增长（1 行修订元数据 +
每个改动分钟 1 行差量），不再复制基础修订的全部分钟；任意历史修订的分钟
范围查询沿基础链还原完整的名义潮位与误差，语义与旧全量复制逐分钟等价：

- **名义潮位更正**：被覆盖分钟 ``level`` 取新值、``error`` 沿用基础修订
  该分钟的误差（基础没有的分钟作为新分钟补入、误差 0）；未列出分钟整行
  继承（允许从非最新修订分叉——未改分钟继承的是**指定**基础修订）。
- **预测误差修订**：被覆盖分钟 ``error`` 取新值、``level`` 沿用基础修订
  该分钟的名义潮位（该分钟在基础修订中必须已有名义潮位——沿基础链还原
  判定——否则 ``MISSING_TIDE_DATA``）；未列出分钟整行继承。

修订内容不可变（差量行一经写入永不修改），规划/确认/批次/改期复核按同一
修订号读到的永远是同一份还原结果；修订生成仍在 ``BEGIN IMMEDIATE`` 事务
内串行生效。

数值有限性
----------
- **写入校验**：名义潮位更正与预测误差修订的每个提交值都必须是**有限数**
  （拒绝 ``1e309`` 这类 JSON 解析出的 Infinity 以及 NaN），误差仍须非负；
  任一值非法即 ``422 VALIDATION_ERROR``，整次不生成修订。
- **历史异常**：已存修订若含非有限值（本修复前写入），一律**不改写历史**；
  查询、单船/批次规划、确认、潮位复核与改期计算命中异常分钟时，以
  ``422 INVALID_TIDE_DATA``（给出修订号与分钟）拒绝，绝不按安全处理、
  绝不报 500。从旧修订分叉时，异常字段被有限新值覆盖的分钟即恢复；其余
  字段与未覆盖分钟仍按原规则继承（异常随之带入新修订，读取时同样拒绝）。
"""
import math
from typing import Iterable, Optional

from ..errors import ApiError, tide_invalid


# ---- 读取（沿基础链还原） -------------------------------------------------------

def revision_chain(conn, revision: int) -> list[tuple[int, Optional[int]]]:
    """沿 ``base_revision`` 从本修订走到物化根。

    返回 ``[(修订号, 基础修订号), ...]``：本修订在前、物化根（基础为
    ``NULL``）在末；修订不存在时返回空列表（调用方负责 404）。
    """
    rows = conn.execute(
        """
        WITH RECURSIVE chain(revision, base_revision, depth) AS (
            SELECT revision, base_revision, 0
            FROM tide_revisions WHERE revision = ?
            UNION ALL
            SELECT r.revision, r.base_revision, c.depth + 1
            FROM tide_revisions r
            JOIN chain c ON r.revision = c.base_revision
        )
        SELECT revision, base_revision FROM chain ORDER BY depth
        """,
        (revision,),
    ).fetchall()
    return [(r["revision"], r["base_revision"]) for r in rows]


def _reconstruct(conn, revision: int, filter_sql: str, params: tuple
                 ) -> dict[int, tuple[float, float]]:
    """按 ``filter_sql``（附加在分钟上的过滤条件）还原某修订的逐分钟值。

    物化根的整行数据打底，其上各差量修订自老到新逐层覆盖（每层只含该次
    改动的分钟，行内即本修订下的最终值）。
    """
    chain = revision_chain(conn, revision)
    result: dict[int, tuple[float, float]] = {}
    if not chain:
        return result

    # 1) 物化根（base_revision 为 NULL）的全量分钟打底
    root_rev = chain[-1][0]
    for r in conn.execute(
        f"SELECT minute, level, error FROM tide_values"
        f" WHERE revision = ? AND {filter_sql}",
        (root_rev, *params),
    ).fetchall():
        result[r["minute"]] = (r["level"], r["error"])

    # 2) 差量修订自老到新逐层覆盖
    delta_revs = [rev for rev, _ in reversed(chain[:-1])]   # 自老到新
    if delta_revs:
        placeholders = ",".join("?" for _ in delta_revs)
        rows = conn.execute(
            f"SELECT revision, minute, level, error FROM tide_deltas"
            f" WHERE revision IN ({placeholders}) AND {filter_sql}",
            (*delta_revs, *params),
        ).fetchall()
        by_rev: dict[int, list] = {}
        for row in rows:
            by_rev.setdefault(row["revision"], []).append(row)
        for rev in delta_revs:
            for row in by_rev.get(rev, ()):
                result[row["minute"]] = (row["level"], row["error"])
    return result


def load_tide_range(conn, revision: int, lo: int, hi: int
                    ) -> dict[int, tuple[float, float]]:
    """还原某修订在 ``[lo, hi]`` 闭区间内的 ``{minute: (名义潮位, 误差)}``。

    供分钟范围查询与规划/确认/批次/改期复核读取；缺名义潮位的分钟不出现在
    结果中（调用方按既有口径报 ``MISSING_TIDE_DATA``）。
    """
    return _reconstruct(conn, revision, "minute BETWEEN ? AND ?", (lo, hi))


def resolve_tide_minutes(conn, revision: int, minutes: Iterable[int]
                         ) -> dict[int, tuple[float, float]]:
    """还原某修订在指定分钟集合上的 ``{minute: (名义潮位, 误差)}``。

    写路径继承判定用：更正需取基础修订被覆盖分钟的误差，误差修订需取基础
    修订被覆盖分钟的名义潮位并校验其存在。
    """
    unique = sorted({int(m) for m in minutes})
    if not unique:
        return {}
    placeholders = ",".join("?" for _ in unique)
    return _reconstruct(conn, revision, f"minute IN ({placeholders})", tuple(unique))


# ---- 数值有限性 -----------------------------------------------------------------

def _is_finite_num(v) -> bool:
    """是否为有限数值（拒绝 Infinity/-Infinity/NaN 及非数值）。"""
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v)


def nonfinite_repr(v) -> str:
    """非有限数值的可 JSON 化字符串表示（Infinity/-Infinity/NaN/null）。

    错误响应 details 只能携带可序列化内容——原始 inf/NaN 会让响应序列化
    再次失败（500），故一律转为字符串。
    """
    if v is None:
        return "null"
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "Infinity" if v > 0 else "-Infinity"
    return str(v)


def validate_submitted_levels(corrections: dict[int, float]) -> None:
    """名义潮位更正提交值校验：每个潮位必须为有限数（米）。

    任一值非法即 ``422 VALIDATION_ERROR`` 且整次不生成修订（调用方在任何
    写库动作之前执行本校验）。
    """
    bad = [
        {"minute": m, "level": nonfinite_repr(v)}
        for m, v in sorted(corrections.items()) if not _is_finite_num(v)
    ]
    if bad:
        raise ApiError(422, "VALIDATION_ERROR",
                       "名义潮位必须为有限数（米），不接受 Infinity/NaN",
                       {"bad_levels": bad})


def validate_submitted_errors(errors: dict[int, float]) -> None:
    """预测误差修订提交值校验：每个误差必须为**有限的非负数**（米）。

    任一值非法即 ``422 VALIDATION_ERROR`` 且整次不生成修订。
    """
    bad = []
    for m, e in sorted(errors.items()):
        if not _is_finite_num(e):
            bad.append({"minute": m, "error": nonfinite_repr(e)})
        elif e < 0:
            bad.append({"minute": m, "error": e})
    if bad:
        raise ApiError(422, "VALIDATION_ERROR",
                       "潮位误差必须为有限的非负数（米），不接受 Infinity/NaN 或负值",
                       {"bad_errors": bad})


def find_invalid_tide(tide: dict[int, tuple[float, float]]
                      ) -> list[tuple[int, dict]]:
    """扫描还原结果中的非有限数值分钟（历史遗留异常数据）。

    返回 ``[(minute, {"level"/"error": 字符串表示}), ...]`` ，按分钟升序；
    空列表表示全部有限。仅检测，不改写任何已存数据。
    """
    bad: list[tuple[int, dict]] = []
    for m in sorted(tide):
        level, error = tide[m]
        fields = {}
        if not _is_finite_num(level):
            fields["level"] = nonfinite_repr(level)
        if not _is_finite_num(error):
            fields["error"] = nonfinite_repr(error)
        if fields:
            bad.append((m, fields))
    return bad


def ensure_finite_tide(revision: int, tide: dict[int, tuple[float, float]]) -> None:
    """还原数据含非有限数值时拒绝（``422 INVALID_TIDE_DATA``，附修订号与分钟）。

    历史遗留异常不改写；查询/规划/确认/复核/改期命中异常分钟一律拒绝，
    绝不按安全处理（如把 Infinity 潮位判为可通航），也绝不把不可 JSON 化
    的数值带进响应（避免 500）。
    """
    bad = find_invalid_tide(tide)
    if bad:
        raise tide_invalid(revision, bad)


# ---- 写入（差量落库） -----------------------------------------------------------

def resolve_base_revision(conn, base_revision: Optional[int]) -> int:
    """解析基础修订号：显式指定须存在，否则取最新修订；否则 TIDE_REVISION_NOT_FOUND。"""
    if base_revision is not None:
        base = base_revision
        if not conn.execute(
            "SELECT 1 FROM tide_revisions WHERE revision = ?", (base,)
        ).fetchone():
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           f"基础修订不存在: {base}", {"revision": base})
    else:
        row = conn.execute(
            "SELECT revision FROM tide_revisions ORDER BY revision DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ApiError(404, "TIDE_REVISION_NOT_FOUND",
                           "系统中尚无任何潮位修订，首次修订必须直接录入")
        base = row["revision"]
    return base


def _insert_delta_revision(conn, base: int, note: str,
                           deltas: dict[int, tuple[float, float]]) -> int:
    """写入一个差量修订：1 行 ``tide_revisions`` + 每个改动分钟 1 行
    ``tide_deltas``（行内为该分钟在本修订下的最终值）。

    持久化写入量只随本次改动分钟数增长，与基础修订的规模无关。调用方须已
    持有写事务；返回新修订号。
    """
    new_rev = conn.execute(
        "INSERT INTO tide_revisions(base_revision, note) VALUES (?, ?)",
        (base, note),
    ).lastrowid
    conn.executemany(
        "INSERT INTO tide_deltas(revision, minute, level, error)"
        " VALUES (?, ?, ?, ?)",
        [(new_rev, m, lvl, err) for m, (lvl, err) in sorted(deltas.items())],
    )
    return new_rev


def create_correction_revision(conn, corrections: dict[int, float],
                               base_revision: Optional[int],
                               note: Optional[str]) -> dict:
    """名义潮位更正（单个 ``BEGIN IMMEDIATE`` 事务，差量落库）。

    只覆盖所列分钟的名义潮位：被覆盖分钟的误差沿用基础修订（沿基础链还原
    取得），基础修订没有的分钟作为新分钟补入、误差 0；未列出分钟整行继承。
    允许从非最新修订分叉。提交值必须全为有限数，否则
    ``422 VALIDATION_ERROR`` 且整次不生成修订。返回新修订行
    （revision/note/created_at）。
    """
    validate_submitted_levels(corrections)
    conn.execute("BEGIN IMMEDIATE")
    try:
        base = resolve_base_revision(conn, base_revision)
        inherited = resolve_tide_minutes(conn, base, corrections.keys())
        deltas = {
            m: (lvl, inherited[m][1] if m in inherited else 0.0)
            for m, lvl in corrections.items()
        }
        new_rev = _insert_delta_revision(
            conn, base, note or f"基于修订 #{base} 的名义潮位更正", deltas
        )
        row = conn.execute(
            "SELECT revision, note, created_at FROM tide_revisions"
            " WHERE revision = ?",
            (new_rev,),
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return dict(row)


def create_error_revision(conn, errors: dict[int, float],
                          base_revision: Optional[int],
                          note: Optional[str]) -> dict:
    """预测误差修订（单个 ``BEGIN IMMEDIATE`` 事务，差量落库）。

    名义潮位不变，只覆盖所列分钟的误差；所列分钟在基础修订中必须已有名义
    潮位（沿基础链还原判定），否则 ``MISSING_TIDE_DATA`` 且不产生新修订；
    未列出分钟整行继承。提交误差必须全为有限的非负数，否则
    ``422 VALIDATION_ERROR`` 且整次不生成修订。返回新修订行
    （revision/note/created_at）。
    """
    validate_submitted_errors(errors)
    conn.execute("BEGIN IMMEDIATE")
    try:
        base = resolve_base_revision(conn, base_revision)
        inherited = resolve_tide_minutes(conn, base, errors.keys())
        missing = [m for m in sorted(errors) if m not in inherited]
        if missing:
            raise ApiError(
                422, "MISSING_TIDE_DATA",
                f"基础修订 #{base} 中有 {len(missing)} 个分钟缺少名义潮位，"
                "误差修订不能为其指定误差",
                {"missing_minutes": missing[:20], "total_missing": len(missing),
                 "base_revision": base},
            )
        deltas = {m: (inherited[m][0], e) for m, e in errors.items()}
        new_rev = _insert_delta_revision(
            conn, base, note or f"基于修订 #{base} 的潮位预测误差修订", deltas
        )
        row = conn.execute(
            "SELECT revision, note, created_at FROM tide_revisions"
            " WHERE revision = ?",
            (new_rev,),
        ).fetchone()
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return dict(row)
