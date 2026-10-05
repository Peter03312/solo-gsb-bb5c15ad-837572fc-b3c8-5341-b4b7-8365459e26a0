"""通行费金额边界（任意精度整数分）测试。

规则（本轮修复）：
* 合法费率为任意非负整数（分/分钟），不设上限；费率版本、开单与作废冲销的
  金额全程保持整数分精确——不溢出、不截断、不经浮点舍入；
* int64 上限费率（9223372036854775807）可建版；其与固化航时的乘积、逐段
  求和、等额负数冲销越 int64 也精确（如 2 分钟行程总额
  18446744073709551614）；
* 超出 int64 的更大合法费率（如 9223372036854775808、10**30）同样可建版、
  开单与冲销；
* 账单仍取开单时最新费率版本与确认快照的进入分钟、固化航时；旧账单及冲销
  不随后续费率变化；
* 非法区间/开单/作废失败不留部分版本、账单或冲销；重复开单/作废与并发
  语义不变；
* 旧库（INTEGER 金额列）升级后既有版本/账单/冲销原样可查，迁移幂等、
  中断可愈合。
"""
import json
import sqlite3

from app.database import connect, init_db

from conftest import add_node, add_segment, seed_initial_revision

I64_MAX = 9223372036854775807


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _setup(client):
    """A->B (seg1, 航时 2), B->C (seg2, 航时 3)；修订覆盖 0..1439。"""
    add_node(client, "A")
    add_node(client, "B")
    add_node(client, "C")
    s1 = add_segment(client, "A", "B", travel_time=2, depth=99.0)
    s2 = add_segment(client, "B", "C", travel_time=3, depth=99.0)
    rev = seed_initial_revision(_level(0, 1439, 3.0))
    return rev, s1, s2


def _plan_confirm(client, rev, origin="A", destination="B", start=0, window=200):
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
# 1. int64 上限费率：建版成功；2 分钟行程开单总额越 int64 仍精确
# ---------------------------------------------------------------------------

def test_int64_max_rate_two_minute_trip_bills_exactly(client):
    rev, s1, s2 = _setup(client)
    v = _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": I64_MAX}])
    assert v["version"] == 1
    assert v["rates"][0]["rate_cents_per_minute"] == I64_MAX

    trip = _plan_confirm(client, rev)  # seg1@0 航时 2
    assert [(l["seg_id"], l["start_minute"], l["travel_time"])
            for l in trip["itinerary"]] == [(s1, 0, 2)]

    r = client.post(f"/api/trips/{trip['trip_id']}/bill")
    assert r.status_code == 201, r.text
    bill = r.json()
    # 2 分钟 × 9223372036854775807 = 18446744073709551614（越 int64，精确）
    assert bill["total_cents"] == 2 * I64_MAX == 18446744073709551614
    seg = bill["segments"][0]
    assert seg["rate_cents_per_minute"] == I64_MAX
    assert seg["amount_cents"] == 2 * I64_MAX
    assert bill["version"] == 1

    # 重复开单重放：同一精确总额（200 replayed=true）
    r2 = client.post(f"/api/trips/{trip['trip_id']}/bill")
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["total_cents"] == 18446744073709551614

    # 各查询接口均精确可读（JSON 整数，不转浮点）
    g = client.get(f"/api/trips/{trip['trip_id']}/bill").json()
    assert g["total_cents"] == 18446744073709551614
    bills = client.get("/api/toll/bills").json()
    assert bills[0]["total_cents"] == 18446744073709551614
    gv = client.get("/api/toll/rate-versions/1").json()
    assert gv["rates"][0]["rate_cents_per_minute"] == I64_MAX
    # 原始响应体是精确十进制文本（未经浮点化）
    raw = client.get(f"/api/trips/{trip['trip_id']}/bill").text
    assert "18446744073709551614" in raw


def test_int64_max_rate_void_reversal_exact(client):
    rev, s1, s2 = _setup(client)
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": I64_MAX}])
    trip = _plan_confirm(client, rev)
    client.post(f"/api/trips/{trip['trip_id']}/bill")

    vr = client.post(f"/api/trips/{trip['trip_id']}/void",
                     json={"reason": "误确认撤销"})
    assert vr.status_code == 201

    # 等额冲销：总额与逐段金额取账单相反数（越 int64 的负数同样精确）
    rr = client.get(f"/api/trips/{trip['trip_id']}/reversal")
    assert rr.status_code == 200
    reversal = rr.json()
    assert reversal["total_cents"] == -18446744073709551614
    assert reversal["segments"][0]["amount_cents"] == -2 * I64_MAX
    assert reversal["segments"][0]["rate_cents_per_minute"] == I64_MAX
    assert reversal["version"] == 1

    # 账单不随作废改变，内嵌同一冲销单
    g = client.get(f"/api/trips/{trip['trip_id']}/bill").json()
    assert g["total_cents"] == 18446744073709551614
    assert g["reversal"]["total_cents"] == -18446744073709551614

    # 重复作废不重复冲销（200 replayed，冲销仍一张）
    again = client.post(f"/api/trips/{trip['trip_id']}/void",
                        json={"reason": "重复"})
    assert again.status_code == 200 and again.json()["replayed"] is True
    conn = connect()
    n = conn.execute("SELECT COUNT(*) FROM toll_reversals").fetchone()[0]
    stored = conn.execute("SELECT total_cents FROM toll_reversals").fetchone()[0]
    conn.close()
    assert n == 1
    assert stored == "-18446744073709551614"  # 十进制文本精确落库


# ---------------------------------------------------------------------------
# 2. 超出 int64 的更大合法费率：建版、开单、冲销均精确
# ---------------------------------------------------------------------------

def test_rate_above_int64_max_accepted_and_billed_exactly(client):
    rev, s1, s2 = _setup(client)
    big1 = I64_MAX + 1          # 9223372036854775808（int64 装不下）
    big2 = 10 ** 30             # 更大的合法费率
    v1 = _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": big1}])
    assert v1["rates"][0]["rate_cents_per_minute"] == big1
    v2 = _put_rates(client, s2, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": big2}])
    # 继承：版本 2 同时含 seg1 的大费率
    rates = {(r["seg_id"]): r["rate_cents_per_minute"] for r in v2["rates"]}
    assert rates[s1] == big1 and rates[s2] == big2

    # A->C：seg1@0 航时 2 × big1 + seg2@2 航时 3 × big2
    trip = _plan_confirm(client, rev, destination="C")
    r = client.post(f"/api/trips/{trip['trip_id']}/bill")
    assert r.status_code == 201, r.text
    bill = r.json()
    expected_segments = [2 * big1, 3 * big2]
    assert [s["amount_cents"] for s in bill["segments"]] == expected_segments
    assert bill["total_cents"] == sum(expected_segments) == 2 * big1 + 3 * big2
    assert bill["version"] == 2

    # 作废 -> 等额冲销精确
    client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": "x"})
    reversal = client.get(f"/api/trips/{trip['trip_id']}/reversal").json()
    assert reversal["total_cents"] == -(2 * big1 + 3 * big2)
    assert [s["amount_cents"] for s in reversal["segments"]] == \
        [-a for a in expected_segments]


def test_segment_amounts_fit_int64_but_sum_overflows(client):
    """各段金额都在 int64 内、求和越界：逐段求和仍精确。"""
    rev, s1, s2 = _setup(client)
    r1, r2 = 4 * 10 ** 18, 2 * 10 ** 18
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100, "rate_cents_per_minute": r1}])
    _put_rates(client, s2, [
        {"start_minute": 0, "end_minute": 100, "rate_cents_per_minute": r2}])
    trip = _plan_confirm(client, rev, destination="C")
    bill = client.post(f"/api/trips/{trip['trip_id']}/bill").json()
    amounts = [2 * r1, 3 * r2]           # 8e18 与 6e18，各自 < int64 上限
    assert all(a <= I64_MAX for a in amounts)
    assert [s["amount_cents"] for s in bill["segments"]] == amounts
    # 总额 14e18 > int64 上限，精确不溢出
    assert bill["total_cents"] == 14000000000000000000


def test_zero_rate_with_any_travel_time_bills_zero_and_reverses_zero(client):
    rev, s1, s2 = _setup(client)
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100, "rate_cents_per_minute": 0}])
    trip = _plan_confirm(client, rev)
    bill = client.post(f"/api/trips/{trip['trip_id']}/bill").json()
    assert bill["total_cents"] == 0
    assert bill["segments"][0]["amount_cents"] == 0

    client.post(f"/api/trips/{trip['trip_id']}/void", json={"reason": "x"})
    reversal = client.get(f"/api/trips/{trip['trip_id']}/reversal").json()
    assert reversal["total_cents"] == 0          # 非正：0 的相反数仍是 0
    assert reversal["segments"][0]["amount_cents"] == 0
    conn = connect()
    stored = conn.execute("SELECT total_cents FROM toll_reversals").fetchone()[0]
    conn.close()
    assert stored == "0"                          # 不是 '-0'


# ---------------------------------------------------------------------------
# 3. 版本固化与拒绝口径不变
# ---------------------------------------------------------------------------

def test_old_bill_unaffected_by_later_huge_rate_versions(client):
    rev, s1, s2 = _setup(client)
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100, "rate_cents_per_minute": 5}])
    trip1 = _plan_confirm(client, rev)
    b1 = client.post(f"/api/trips/{trip1['trip_id']}/bill").json()
    assert b1["version"] == 1 and b1["total_cents"] == 10

    # 之后出现超大费率版本：旧账单与旧版本查询都不变
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100,
         "rate_cents_per_minute": 10 ** 25}])
    g = client.get(f"/api/trips/{trip1['trip_id']}/bill").json()
    assert g["version"] == 1 and g["total_cents"] == 10
    gv = client.get("/api/toll/rate-versions/1").json()
    assert gv["rates"][0]["rate_cents_per_minute"] == 5

    # 新行程按最新版本（超大费率）精确开单（避开 trip1 的 0..1 占用，分钟 2 进入）
    trip2 = _plan_confirm(client, rev, start=0)
    assert trip2["itinerary"][0]["start_minute"] == 2
    b2 = client.post(f"/api/trips/{trip2['trip_id']}/bill").json()
    assert b2["version"] == 2 and b2["total_cents"] == 2 * 10 ** 25


def test_invalid_rates_rejected_without_partial_version(client):
    rev, s1, s2 = _setup(client)
    _put_rates(client, s1, [
        {"start_minute": 0, "end_minute": 100, "rate_cents_per_minute": 7}])

    bad = [
        # 负费率（含越 int64 的大负数）
        [{"start_minute": 0, "end_minute": 10, "rate_cents_per_minute": -1}],
        [{"start_minute": 0, "end_minute": 10,
          "rate_cents_per_minute": -(2 * I64_MAX)}],
        # 起点为负 / 终点不大于起点（大费率不影响区间校验）
        [{"start_minute": -1, "end_minute": 10,
          "rate_cents_per_minute": 10 ** 30}],
        [{"start_minute": 5, "end_minute": 5,
          "rate_cents_per_minute": 10 ** 30}],
    ]
    for rates in bad:
        r = client.put(f"/api/segments/{s1}/toll-rates", json={"rates": rates})
        assert r.status_code == 422, r.text
        assert r.json()["error"]["code"] == "INVALID_TOLL_RATE_INTERVAL"

    # 大费率组内交叠同样整次拒绝
    r = client.put(f"/api/segments/{s1}/toll-rates", json={"rates": [
        {"start_minute": 0, "end_minute": 60, "rate_cents_per_minute": 10 ** 30},
        {"start_minute": 59, "end_minute": 90, "rate_cents_per_minute": 1},
    ]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "OVERLAPPING_TOLL_RATE_INTERVAL"

    # 非整数费率（浮点）拒绝
    r = client.put(f"/api/segments/{s1}/toll-rates", json={"rates": [
        {"start_minute": 0, "end_minute": 10, "rate_cents_per_minute": 1.5}]})
    assert r.status_code == 422

    # 任一失败整次拒绝：仍只有版本 1，内容不变
    versions = client.get("/api/toll/rate-versions").json()
    assert [v["version"] for v in versions] == [1]
    assert versions[0]["rates"][0]["rate_cents_per_minute"] == 7


# ---------------------------------------------------------------------------
# 4. 旧库（INTEGER 金额列）升级：既有数据保留可查，迁移幂等
# ---------------------------------------------------------------------------

# 本轮修复前的库结构：三张通行费表的金额列为 INTEGER（int64）
_LEGACY_SCHEMA = """
CREATE TABLE nodes (node_id TEXT PRIMARY KEY, name TEXT,
 created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE segments (seg_id INTEGER PRIMARY KEY AUTOINCREMENT,
 origin TEXT NOT NULL, destination TEXT NOT NULL,
 travel_time INTEGER NOT NULL CHECK (travel_time > 0),
 depth REAL NOT NULL, clearance REAL,
 created_at TEXT NOT NULL DEFAULT (datetime('now')), CHECK (origin <> destination));
CREATE TABLE tide_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT,
 created_at TEXT NOT NULL DEFAULT (datetime('now')), note TEXT);
CREATE TABLE tide_values (revision INTEGER NOT NULL, minute INTEGER NOT NULL,
 level REAL NOT NULL, PRIMARY KEY (revision, minute));
CREATE TABLE confirmed_trips (trip_id INTEGER PRIMARY KEY AUTOINCREMENT,
 revision INTEGER NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,
 start_minute INTEGER NOT NULL, window_minutes INTEGER NOT NULL,
 draft REAL NOT NULL, height REAL NOT NULL, margin REAL NOT NULL,
 arrival_minute INTEGER NOT NULL, itinerary TEXT NOT NULL,
 confirmed_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE toll_rate_versions (version INTEGER PRIMARY KEY AUTOINCREMENT,
 changed_seg_id INTEGER NOT NULL, note TEXT,
 created_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE toll_rates (version INTEGER NOT NULL, seg_id INTEGER NOT NULL,
 start_minute INTEGER NOT NULL CHECK (start_minute >= 0),
 end_minute INTEGER NOT NULL CHECK (end_minute > start_minute),
 rate_cents_per_minute INTEGER NOT NULL CHECK (rate_cents_per_minute >= 0),
 PRIMARY KEY (version, seg_id, start_minute));
CREATE TABLE toll_bills (trip_id INTEGER PRIMARY KEY, version INTEGER NOT NULL,
 total_cents INTEGER NOT NULL CHECK (total_cents >= 0),
 segments TEXT NOT NULL,
 billed_at TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE toll_reversals (bill_trip_id INTEGER PRIMARY KEY,
 version INTEGER NOT NULL,
 total_cents INTEGER NOT NULL CHECK (total_cents <= 0),
 segments TEXT NOT NULL,
 reversed_at TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _build_legacy_db(db_file):
    """构造 INTEGER 金额列时代的旧库：费率版本 #1（seg1 [0,100) 2 分/分钟）、
    一条已确认行程（seg1@0 航时 2）、其账单（总额 20）与等额冲销（-20）。"""
    conn = sqlite3.connect(db_file)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute("INSERT INTO nodes(node_id, name) VALUES ('A', '外锚地')")
    conn.execute("INSERT INTO nodes(node_id, name) VALUES ('B', '内港')")
    conn.execute("INSERT INTO segments(origin, destination, travel_time, depth)"
                 " VALUES ('A', 'B', 2, 99.0)")
    conn.execute("INSERT INTO tide_revisions(revision, note) VALUES (1, '旧修订')")
    conn.executemany("INSERT INTO tide_values(revision, minute, level)"
                     " VALUES (1, ?, 3.0)", [(m,) for m in range(100)])
    itinerary = [{"seg_id": 1, "origin": "A", "destination": "B",
                  "start_minute": 0, "end_minute": 2, "travel_time": 2}]
    conn.execute(
        "INSERT INTO confirmed_trips(revision, origin, destination, start_minute,"
        " window_minutes, draft, height, margin, arrival_minute, itinerary)"
        " VALUES (1, 'A', 'B', 0, 100, 1.0, 0.0, 0.0, 2, ?)",
        (json.dumps(itinerary),),
    )
    conn.execute("INSERT INTO toll_rate_versions(version, changed_seg_id, note)"
                 " VALUES (1, 1, '旧费率版本')")
    conn.execute("INSERT INTO toll_rates(version, seg_id, start_minute, end_minute,"
                 " rate_cents_per_minute) VALUES (1, 1, 0, 100, 2)")
    segments = [{"leg_index": 0, "seg_id": 1, "origin": "A", "destination": "B",
                 "entry_minute": 0, "travel_time": 2,
                 "rate_cents_per_minute": 2, "amount_cents": 20}]
    conn.execute("INSERT INTO toll_bills(trip_id, version, total_cents, segments)"
                 " VALUES (1, 1, 20, ?)", (json.dumps(segments),))
    reversal = [{**s, "amount_cents": -20} for s in segments]
    conn.execute("INSERT INTO toll_reversals(bill_trip_id, version, total_cents,"
                 " segments) VALUES (1, 1, -20, ?)", (json.dumps(reversal),))
    conn.commit()
    conn.close()


def _declared_types(db_file):
    conn = sqlite3.connect(db_file)
    types = {}
    for table, col in (("toll_rates", "rate_cents_per_minute"),
                       ("toll_bills", "total_cents"),
                       ("toll_reversals", "total_cents")):
        for r in conn.execute(f"PRAGMA table_info({table})").fetchall():
            if r[1] == col:
                types[table] = r[2].upper()
    conn.close()
    return types


def test_legacy_db_integer_money_columns_migrate_to_text(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from app import config
    from app.main import app

    db_file = tmp_path / "legacy-toll.db"
    _build_legacy_db(db_file)
    assert _declared_types(db_file) == {
        "toll_rates": "INTEGER", "toll_bills": "INTEGER",
        "toll_reversals": "INTEGER"}

    monkeypatch.setattr(config, "DB_PATH", str(db_file))
    monkeypatch.setattr(config, "SEED_DEMO", False)
    conn = connect()
    init_db(conn)
    init_db(conn)  # 幂等：重复执行不报错、不重复迁移
    conn.close()

    # 金额列已重建为 TEXT；既有值原样保留（十进制文本）
    assert _declared_types(db_file) == {
        "toll_rates": "TEXT", "toll_bills": "TEXT", "toll_reversals": "TEXT"}
    conn = connect()
    assert conn.execute("SELECT rate_cents_per_minute FROM toll_rates"
                        ).fetchone()[0] == "2"
    assert conn.execute("SELECT total_cents FROM toll_bills").fetchone()[0] == "20"
    assert conn.execute("SELECT total_cents FROM toll_reversals"
                        ).fetchone()[0] == "-20"
    conn.close()

    with TestClient(app) as client:
        # 既有版本、账单、冲销照常可查（HTTP 层仍是精确 JSON 整数）
        v = client.get("/api/toll/rate-versions/1").json()
        assert v["rates"][0]["rate_cents_per_minute"] == 2
        bill = client.get("/api/trips/1/bill").json()
        assert bill["total_cents"] == 20
        assert bill["reversal"]["total_cents"] == -20
        reversal = client.get("/api/trips/1/reversal").json()
        assert reversal["total_cents"] == -20

        # 升级后的库可提交越 int64 的大费率并精确开单/冲销
        r = client.put("/api/segments/1/toll-rates", json={"rates": [
            {"start_minute": 0, "end_minute": 100,
             "rate_cents_per_minute": I64_MAX + 5}]})
        assert r.status_code == 201, r.text
        assert r.json()["version"] == 2

        p = client.post("/api/trips/plan", json={
            "origin": "A", "destination": "B", "start_minute": 0,
            "window_minutes": 100, "draft": 1.0, "height": 0.0,
            "margin": 0.0, "revision": 1})
        assert p.status_code == 200, p.text
        c = client.post("/api/trips/confirm", json=p.json())
        assert c.status_code == 201, c.text
        tid = c.json()["trip_id"]

        b = client.post(f"/api/trips/{tid}/bill")
        assert b.status_code == 201, b.text
        assert b.json()["total_cents"] == 2 * (I64_MAX + 5)
        client.post(f"/api/trips/{tid}/void", json={"reason": "升级后作废"})
        rr = client.get(f"/api/trips/{tid}/reversal").json()
        assert rr["total_cents"] == -2 * (I64_MAX + 5)

        # 旧账单仍是旧版本旧金额（不随新费率改变）
        assert client.get("/api/trips/1/bill").json()["total_cents"] == 20
