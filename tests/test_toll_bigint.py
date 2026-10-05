"""通行费金额边界（任意精度整数）测试。

修复前：费率列与金额列是 SQLite 64 位 INTEGER——提交 2^63-1 分/分钟的
费率可建版，但两分钟行程开单时乘积越界报 500；更大的合法费率（>= 2^63）
连版本都提交失败。

修复后：合法非负整数费率（任意大）及其与固化航时的乘积、逐段求和、等额
负数冲销均保持整数分精确——不溢出、不截断、不浮点舍入。金额列以 TEXT
十进制字符串存储（旧库启动时自动重建迁移），既有版本、账单、冲销数值
不变、仍可查询；请求/响应字段与状态码对正常范围保持兼容。
"""
import json
import threading

from app import config
from app.database import SCHEMA, connect, init_db
from app.main import app

from conftest import add_node, add_segment, seed_initial_revision

from fastapi.testclient import TestClient

INT64_MAX = 9223372036854775807          # 2^63 - 1
BEYOND_INT64 = 9223372036854775808       # 2^63（修复前连版本都提交失败）
HUGE = 10**30 + 123456789                # 远超 64 位的合法非负整数费率


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _setup_two_leg(client):
    """A->B (seg1, 航时 10), B->C (seg2, 航时 20)；修订覆盖 0..1439。"""
    add_node(client, "A")
    add_node(client, "B")
    add_node(client, "C")
    s1 = add_segment(client, "A", "B", travel_time=10, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=20, depth=99.0)
    rev = seed_initial_revision(_level(0, 1439, 3.0))
    return rev, s1, s2


def _plan_confirm(client, rev, origin="A", destination="C", start=0, window=200):
    body = {"origin": origin, "destination": destination,
            "start_minute": start, "window_minutes": window,
            "draft": 1.0, "height": 0.0, "margin": 0.0, "revision": rev}
    p = client.post("/api/trips/plan", json=body)
    assert p.status_code == 200, p.text
    c = client.post("/api/trips/confirm", json=p.json())
    assert c.status_code == 201, c.text
    return c.json()


def _put_rates(client, seg_id, rates, expected=201):
    r = client.put(f"/api/segments/{seg_id}/toll-rates", json={"rates": rates})
    assert r.status_code == expected, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. 边界费率建版：2^63-1 / 2^63 / 任意大整数均精确保存
# ---------------------------------------------------------------------------

def test_max_int64_rate_version_and_query_exact(client):
    rev, s1, s2 = _setup_two_leg(client)
    v = _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": INT64_MAX}])
    assert v["rates"][0]["rate_cents_per_minute"] == INT64_MAX

    # 指定版本与全量版本查询均精确回读
    g = client.get("/api/toll/rate-versions/1").json()
    assert g["rates"][0]["rate_cents_per_minute"] == INT64_MAX
    allv = client.get("/api/toll/rate-versions").json()
    assert allv[0]["rates"][0]["rate_cents_per_minute"] == INT64_MAX


def test_rate_beyond_int64_version_accepted(client):
    """2^63（合法非负整数，超出 64 位）建版成功且精确可查。"""
    rev, s1, s2 = _setup_two_leg(client)
    r = client.put(f"/api/segments/{s1}/toll-rates", json={"rates": [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": BEYOND_INT64}]})
    assert r.status_code == 201, r.text
    assert r.json()["rates"][0]["rate_cents_per_minute"] == BEYOND_INT64

    g = client.get("/api/toll/rate-versions/1").json()
    assert g["rates"][0]["rate_cents_per_minute"] == BEYOND_INT64


def test_huge_rate_and_inheritance_exact(client):
    """任意大费率建版精确；后续版本整组继承时大费率不截断、不舍入。"""
    rev, s1, s2 = _setup_two_leg(client)
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 1440,
         "rate_cents_per_minute": HUGE}])
    # 对 seg2 提交新版本：seg1 的超大费率应原样继承到新版本
    v2 = _put_rates(client, s2, [
        {"start_minute": 0, "end_minute": 1440, "rate_cents_per_minute": 1}])
    seg1_rates = [r for r in v2["rates"] if r["seg_id"] == s1]
    assert seg1_rates[0]["rate_cents_per_minute"] == HUGE
    # 历史版本同样精确
    g1 = client.get("/api/toll/rate-versions/1").json()
    assert g1["rates"][0]["rate_cents_per_minute"] == HUGE


# ---------------------------------------------------------------------------
# 2. 开单：乘积 / 逐段求和越界保持整数分精确
# ---------------------------------------------------------------------------

def test_bill_max_int64_rate_two_minute_trip_exact(client):
    """题目场景：2^63-1 分/分钟 × 两分钟行程，乘积越 64 位仍精确开单。"""
    add_node(client, "A")
    add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 1439, 3.0))
    _put_rates(client, seg, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": INT64_MAX}])

    trip = _plan_confirm(client, rev, origin="A", destination="B")
    r = client.post(f"/api/trips/{trip['trip_id']}/bill")
    assert r.status_code == 201, r.text
    bill = r.json()
    expected = 2 * INT64_MAX  # 18446744073709551614，越 64 位
    assert bill["segments"][0]["amount_cents"] == expected
    assert bill["segments"][0]["rate_cents_per_minute"] == INT64_MAX
    assert bill["total_cents"] == expected

    # 查询接口（response_model 序列化路径）同样精确
    g = client.get(f"/api/trips/{trip['trip_id']}/bill")
    assert g.status_code == 200
    assert g.json()["total_cents"] == expected
    listed = client.get("/api/toll/bills").json()
    assert listed[0]["total_cents"] == expected


def test_bill_beyond_int64_rate_and_reversal_exact(client):
    """费率 2^63：开单、重放、等额负数冲销全程精确。"""
    add_node(client, "A")
    add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 1439, 3.0))
    _put_rates(client, seg, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": BEYOND_INT64}])

    trip = _plan_confirm(client, rev, origin="A", destination="B")
    expected = 2 * BEYOND_INT64  # 18446744073709551616

    r = client.post(f"/api/trips/{trip['trip_id']}/bill")
    assert r.status_code == 201, r.text
    assert r.json()["total_cents"] == expected

    # 重复开单重放：返回首次账单，金额精确不变
    r2 = client.post(f"/api/trips/{trip['trip_id']}/bill")
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["total_cents"] == expected

    # 作废 -> 同事务等额冲销：总额与逐段金额为账单相反数，绝对值完全相等
    v = client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": "撤销"})
    assert v.status_code == 201
    rr = client.get(f"/api/trips/{trip['trip_id']}/reversal")
    assert rr.status_code == 200
    reversal = rr.json()
    assert reversal["total_cents"] == -expected
    assert reversal["segments"][0]["amount_cents"] == -expected
    assert reversal["segments"][0]["rate_cents_per_minute"] == BEYOND_INT64
    assert reversal["version"] == 1

    # 账单内嵌冲销单精确；账单本身不被改写
    g = client.get(f"/api/trips/{trip['trip_id']}/bill").json()
    assert g["total_cents"] == expected
    assert g["reversal"]["total_cents"] == -expected


def test_bill_multi_leg_sum_beyond_int64_exact(client):
    """逐段求和：两段各自不越界、合计越 64 位，总额精确。"""
    rev, s1, s2 = _setup_two_leg(client)
    # 快照：seg1@0 航时 10 -> seg2@10 航时 20
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": INT64_MAX}])
    _put_rates(client, s2, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": INT64_MAX}])

    trip = _plan_confirm(client, rev)
    r = client.post(f"/api/trips/{trip['trip_id']}/bill")
    assert r.status_code == 201, r.text
    bill = r.json()
    assert bill["segments"][0]["amount_cents"] == 10 * INT64_MAX
    assert bill["segments"][1]["amount_cents"] == 20 * INT64_MAX
    assert bill["total_cents"] == 30 * INT64_MAX  # 越 64 位的精确和

    # 作废冲销：逐段与总额精确取反
    client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": "x"})
    reversal = client.get(f"/api/trips/{trip['trip_id']}/reversal").json()
    assert reversal["total_cents"] == -(30 * INT64_MAX)
    assert [s["amount_cents"] for s in reversal["segments"]] == [
        -(10 * INT64_MAX), -(20 * INT64_MAX)]


def test_bill_huge_rate_arbitrary_precision(client):
    """任意大费率（10^30 级）：乘积、求和、冲销无截断无浮点舍入。"""
    add_node(client, "A")
    add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=7, depth=99.0)
    rev = seed_initial_revision(_level(0, 1439, 3.0))
    _put_rates(client, seg, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": HUGE}])

    trip = _plan_confirm(client, rev, origin="A", destination="B")
    bill = client.post(f"/api/trips/{trip['trip_id']}/bill").json()
    assert bill["total_cents"] == 7 * HUGE
    assert bill["segments"][0]["amount_cents"] == 7 * HUGE

    client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": "x"})
    reversal = client.get(f"/api/trips/{trip['trip_id']}/reversal").json()
    assert reversal["total_cents"] == -(7 * HUGE)


def test_old_bill_unchanged_after_big_rate_update(client):
    """旧账单不随后续（超大）费率版本改变；新行程按最新版本精确开单。"""
    rev, s1, s2 = _setup_two_leg(client)
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100, "rate_cents_per_minute": 2}])
    t1 = _plan_confirm(client, rev, origin="A", destination="B")
    first = client.post(f"/api/trips/{t1['trip_id']}/bill")
    assert first.status_code == 201
    assert first.json()["total_cents"] == 20  # 10 分钟 × 2

    # 费率涨到 2^63：旧账单原样
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": BEYOND_INT64}])
    g = client.get(f"/api/trips/{t1['trip_id']}/bill").json()
    assert g["version"] == 1
    assert g["total_cents"] == 20

    # 新行程按版本 2 精确开单（航时 10 × 2^63；t1 占用 0..9，本行程分钟 10 进入）
    t2 = _plan_confirm(client, rev, origin="A", destination="B")
    assert t2["itinerary"][0]["start_minute"] == 10
    b2 = client.post(f"/api/trips/{t2['trip_id']}/bill").json()
    assert b2["version"] == 2
    assert b2["total_cents"] == 10 * BEYOND_INT64


def test_uncovered_minutes_rate_zero_with_big_rates(client):
    """未覆盖分钟费率 0：大费率区间外的进入分钟金额仍为 0。"""
    rev, s1, s2 = _setup_two_leg(client)
    _put_rates(client, s1, [
        {"start_minute": 500, "end_minute": 600,
         "rate_cents_per_minute": HUGE}])
    trip = _plan_confirm(client, rev)  # 进入分钟 0 / 10，均未覆盖
    bill = client.post(f"/api/trips/{trip['trip_id']}/bill").json()
    assert bill["total_cents"] == 0
    assert [s["rate_cents_per_minute"] for s in bill["segments"]] == [0, 0]


def test_zero_leg_trip_bill_zero_with_big_rates(client):
    rev, s1, s2 = _setup_two_leg(client)
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": HUGE}])
    trip = _plan_confirm(client, rev, origin="A", destination="A")
    assert trip["itinerary"] == []
    bill = client.post(f"/api/trips/{trip['trip_id']}/bill").json()
    assert bill["total_cents"] == 0
    assert bill["segments"] == []


# ---------------------------------------------------------------------------
# 3. 非法输入拒绝口径不变（不生成版本、不留半成品）
# ---------------------------------------------------------------------------

def test_invalid_rates_rejected_without_version(client):
    rev, s1, s2 = _setup_two_leg(client)
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100, "rate_cents_per_minute": 2}])

    bad = [
        # 负费率（含超大负数）
        [{"start_minute": 0, "end_minute": 10, "rate_cents_per_minute": -HUGE}],
        [{"start_minute": 0, "end_minute": 10, "rate_cents_per_minute": -1}],
        # 非整数费率（小数）
        [{"start_minute": 0, "end_minute": 10, "rate_cents_per_minute": 1.5}],
        # 非法分钟
        [{"start_minute": -1, "end_minute": 10, "rate_cents_per_minute": HUGE}],
        [{"start_minute": 10, "end_minute": 10, "rate_cents_per_minute": HUGE}],
    ]
    for rates in bad:
        r = client.put(f"/api/segments/{s1}/toll-rates", json={"rates": rates})
        assert r.status_code == 422, (rates, r.text)

    # 大费率组内交叠
    r = client.put(f"/api/segments/{s1}/toll-rates", json={"rates": [
        {"start_minute": 0, "end_minute": 60,
         "rate_cents_per_minute": HUGE},
        {"start_minute": 59, "end_minute": 100,
         "rate_cents_per_minute": BEYOND_INT64},
    ]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVERLAPPING_TOLL_RATE_INTERVAL"

    # 整次拒绝：仍只有版本 1
    versions = client.get("/api/toll/rate-versions").json()
    assert [v["version"] for v in versions] == [1]


# ---------------------------------------------------------------------------
# 4. 并发：大金额下重复开单仍一次生效
# ---------------------------------------------------------------------------

def test_concurrent_double_bill_big_rate_single_bill(client):
    add_node(client, "A")
    add_node(client, "B")
    seg = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    rev = seed_initial_revision(_level(0, 1439, 3.0))
    _put_rates(client, seg, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": BEYOND_INT64}])
    trip = _plan_confirm(client, rev, origin="A", destination="B")

    bar = threading.Barrier(2)
    results = []

    def bill():
        bar.wait()
        r = client.post(f"/api/trips/{trip['trip_id']}/bill")
        results.append((r.status_code, r.json()))

    t1 = threading.Thread(target=bill)
    t2 = threading.Thread(target=bill)
    t1.start(); t2.start(); t1.join(); t2.join()

    codes = sorted(r[0] for r in results)
    assert codes == [200, 201]
    expected = 2 * BEYOND_INT64
    assert all(r[1]["total_cents"] == expected for r in results)
    conn = connect()
    assert conn.execute("SELECT COUNT(*) FROM toll_bills").fetchone()[0] == 1
    conn.close()


# ---------------------------------------------------------------------------
# 5. 旧库迁移：INTEGER 金额列重建为 TEXT，既有数据数值不变、可查
# ---------------------------------------------------------------------------

def _build_old_schema_db(db_file):
    """构造修复前（64 位 INTEGER 金额列）的旧库并写入既有费率/账单/冲销。"""
    old_schema = SCHEMA.replace(
        "rate_cents_per_minute TEXT NOT NULL",
        "rate_cents_per_minute INTEGER NOT NULL"
        " CHECK (rate_cents_per_minute >= 0)")
    assert "rate_cents_per_minute INTEGER NOT NULL" in old_schema
    old_schema = old_schema.replace(
        "total_cents   TEXT NOT NULL", "total_cents   INTEGER NOT NULL")
    assert old_schema.count("total_cents   INTEGER NOT NULL") == 2

    conn = connect()
    conn.executescript(old_schema)
    conn.execute("INSERT INTO nodes(node_id, name) VALUES ('A', '外锚地')")
    conn.execute("INSERT INTO nodes(node_id, name) VALUES ('B', '内港')")
    conn.execute(
        "INSERT INTO segments(origin, destination, travel_time, depth)"
        " VALUES ('A', 'B', 2, 99.0)")  # seg_id = 1
    conn.execute(
        "INSERT INTO confirmed_trips"
        " (revision, origin, destination, start_minute, window_minutes,"
        "  draft, height, margin, arrival_minute, itinerary)"
        " VALUES (1, 'A', 'B', 0, 200, 1.0, 0.0, 0.0, 2, ?)",
        (json.dumps([{"seg_id": 1, "origin": "A", "destination": "B",
                      "start_minute": 0, "end_minute": 2, "travel_time": 2}]),),
    )  # trip_id = 1
    conn.execute(
        "INSERT INTO toll_rate_versions(changed_seg_id, note) VALUES (1, '旧版费率')")
    conn.execute(
        "INSERT INTO toll_rates(version, seg_id, start_minute, end_minute,"
        " rate_cents_per_minute) VALUES (1, 1, 0, 100, 5)")
    conn.execute(
        "INSERT INTO toll_bills(trip_id, version, total_cents, segments)"
        " VALUES (1, 1, 10, ?)",
        (json.dumps([{"leg_index": 0, "seg_id": 1, "origin": "A",
                      "destination": "B", "entry_minute": 0, "travel_time": 2,
                      "rate_cents_per_minute": 5, "amount_cents": 10}]),),
    )
    conn.execute("INSERT INTO voided_trips(trip_id, reason) VALUES (1, '旧作废')")
    conn.execute(
        "INSERT INTO toll_reversals(bill_trip_id, version, total_cents, segments)"
        " VALUES (1, 1, -10, ?)",
        (json.dumps([{"leg_index": 0, "seg_id": 1, "origin": "A",
                      "destination": "B", "entry_minute": 0, "travel_time": 2,
                      "rate_cents_per_minute": 5, "amount_cents": -10}]),),
    )
    conn.commit()
    conn.close()


def test_old_database_money_columns_migrated(tmp_path, monkeypatch):
    """旧库（INTEGER 金额列）启动即迁移：既有数据数值不变、可查，
    迁移后超大费率/金额精确工作，重复迁移幂等。"""
    db_file = tmp_path / "old.db"
    monkeypatch.setenv("DB_PATH", str(db_file))
    monkeypatch.setattr(config, "DB_PATH", str(db_file))

    _build_old_schema_db(db_file)

    # 迁移前：金额列确为 INTEGER
    conn = connect()
    assert _col_type(conn, "toll_rates", "rate_cents_per_minute") == "INTEGER"
    assert _col_type(conn, "toll_bills", "total_cents") == "INTEGER"
    assert _col_type(conn, "toll_reversals", "total_cents") == "INTEGER"
    conn.close()

    # 启动迁移（init_db 幂等；连跑两次验证可重入）
    conn = connect()
    init_db(conn)
    init_db(conn)
    # 迁移后：金额列已是 TEXT，既有值 CAST 为十进制文本、数值不变
    assert _col_type(conn, "toll_rates", "rate_cents_per_minute") == "TEXT"
    assert _col_type(conn, "toll_bills", "total_cents") == "TEXT"
    assert _col_type(conn, "toll_reversals", "total_cents") == "TEXT"
    assert conn.execute(
        "SELECT rate_cents_per_minute FROM toll_rates").fetchone()[0] == "5"
    assert conn.execute(
        "SELECT total_cents FROM toll_bills").fetchone()[0] == "10"
    assert conn.execute(
        "SELECT total_cents FROM toll_reversals").fetchone()[0] == "-10"
    conn.close()

    with TestClient(app) as client:
        # 既有版本 / 账单 / 冲销仍可查询，数值不变（JSON 整数口径）
        v = client.get("/api/toll/rate-versions/1")
        assert v.status_code == 200
        assert v.json()["rates"][0]["rate_cents_per_minute"] == 5
        b = client.get("/api/trips/1/bill")
        assert b.status_code == 200
        assert b.json()["total_cents"] == 10
        assert b.json()["reversal"]["total_cents"] == -10
        rr = client.get("/api/trips/1/reversal")
        assert rr.status_code == 200
        assert rr.json()["total_cents"] == -10

        # 迁移后超大费率建版精确（继承旧版本费率）
        r = client.put("/api/segments/1/toll-rates", json={"rates": [
            {"start_minute": 0, "end_minute": 100,
             "rate_cents_per_minute": BEYOND_INT64}]})
        assert r.status_code == 201, r.text
        assert r.json()["rates"][0]["rate_cents_per_minute"] == BEYOND_INT64

        # 迁移后新行程按超大费率精确开单（直接落一条确认快照）
        conn = connect()
        cur = conn.execute(
            "INSERT INTO confirmed_trips"
            " (revision, origin, destination, start_minute, window_minutes,"
            "  draft, height, margin, arrival_minute, itinerary)"
            " VALUES (1, 'A', 'B', 0, 200, 1.0, 0.0, 0.0, 2, ?)",
            (json.dumps([{"seg_id": 1, "origin": "A", "destination": "B",
                          "start_minute": 0, "end_minute": 2,
                          "travel_time": 2}]),),
        )
        new_trip = cur.lastrowid
        conn.commit()
        conn.close()
        nb = client.post(f"/api/trips/{new_trip}/bill")
        assert nb.status_code == 201, nb.text
        assert nb.json()["total_cents"] == 2 * BEYOND_INT64

        # 旧账单不受新费率影响
        assert client.get("/api/trips/1/bill").json()["total_cents"] == 10


def _col_type(conn, table, column):
    for r in conn.execute(f"PRAGMA table_info({table})").fetchall():
        if r["name"] == column:
            return (r["type"] or "").upper()
    return ""
