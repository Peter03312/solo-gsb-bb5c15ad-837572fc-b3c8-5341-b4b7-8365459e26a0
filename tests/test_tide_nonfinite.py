"""潮位数值有限性校验与历史遗留非有限值处理（本轮修复）测试。

背景：JSON 极大数（如 1e309）会被解析为 Infinity 并写入不可变修订，导致
查询该修订报 500、规划把本应受限的分钟误判为可通航。

修复口径：
- 写路径：名义潮位更正与预测误差修订的每个提交值必须为有限数（误差仍须
  非负），否则 422 VALIDATION_ERROR，整次不生成修订；
- 读路径：已存含非有限值的修订不改写历史；查询命中异常分钟，或单船/批次
  规划、确认、潮位复核与改期计算需要该分钟时，一律 422 INVALID_TIDE_DATA
  （附修订号与分钟），绝不按安全处理、绝不报 500；
- 分叉恢复：以旧修订分叉时，只有异常字段被有限新值覆盖才恢复该分钟；
  其余字段及未覆盖分钟仍按原规则继承（异常随之带入新修订）。
"""
from app.database import connect, init_db

from conftest import add_node, add_segment, seed_initial_revision


INF = float("inf")


def _level(lo, hi, v):
    return {m: v for m in range(lo, hi + 1)}


def _post_raw(client, url, body: str):
    """以原始 JSON 文本提交（1e309/NaN 等无法经 json= 序列化的载荷）。"""
    return client.post(
        url, content=body.encode("utf-8"),
        headers={"content-type": "application/json"},
    )


def seed_anomalous_revision(values: dict, note="历史异常修订", base=None) -> int:
    """测试辅助：直接写入含非有限值的修订（模拟本修复前已存在的历史数据）。

    values: {minute: (level, error)}；base 为 None 时写物化修订
    （tide_values），否则写基于 base 的差量修订（tide_deltas）。
    """
    conn = connect()
    try:
        if base is None:
            rev = conn.execute(
                "INSERT INTO tide_revisions(note) VALUES (?)", (note,)
            ).lastrowid
            conn.executemany(
                "INSERT INTO tide_values(revision, minute, level, error)"
                " VALUES (?, ?, ?, ?)",
                [(rev, m, lv, er) for m, (lv, er) in sorted(values.items())],
            )
        else:
            rev = conn.execute(
                "INSERT INTO tide_revisions(base_revision, note) VALUES (?, ?)",
                (base, note),
            ).lastrowid
            conn.executemany(
                "INSERT INTO tide_deltas(revision, minute, level, error)"
                " VALUES (?, ?, ?, ?)",
                [(rev, m, lv, er) for m, (lv, er) in sorted(values.items())],
            )
        return rev
    finally:
        conn.close()


def _revision_ids(client):
    return [x["revision"] for x in client.get("/api/tide/revisions").json()]


# ---------------------------------------------------------------------------
# 1. 写路径：名义潮位更正拒绝非有限值（422 VALIDATION_ERROR，整次不生成修订）
# ---------------------------------------------------------------------------

def test_correction_rejects_huge_json_number(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    r = _post_raw(client, "/api/tide/corrections", '{"corrections": {"5": 1e309}}')
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["bad_levels"] == [
        {"minute": 5, "level": "Infinity"}
    ]
    # 整次不生成修订
    assert _revision_ids(client) == [rev1]


def test_correction_rejects_negative_infinity_and_nan(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    r = _post_raw(client, "/api/tide/corrections",
                  '{"corrections": {"5": -1e309}}')
    assert r.status_code == 422
    assert r.json()["error"]["details"]["bad_levels"] == [
        {"minute": 5, "level": "-Infinity"}
    ]
    r = _post_raw(client, "/api/tide/corrections", '{"corrections": {"5": NaN}}')
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["details"]["bad_levels"] == [
        {"minute": 5, "level": "NaN"}
    ]
    r = _post_raw(client, "/api/tide/corrections",
                  '{"corrections": {"5": Infinity}}')
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _revision_ids(client) == [rev1]


def test_correction_mixed_valid_and_invalid_rejected_atomically(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    # 同一提交里既有合法值也有非有限值：整次拒绝，合法分钟也不落库
    r = _post_raw(client, "/api/tide/corrections",
                  '{"corrections": {"3": 2.0, "5": 1e309}}')
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert _revision_ids(client) == [rev1]
    g = client.get(f"/api/tide/revisions/{rev1}",
                   params={"from_minute": 0, "to_minute": 19}).json()
    assert g["levels"]["3"] == 3.0   # 基础修订原样不变


def test_correction_finite_values_still_accepted(client):
    seed_initial_revision(_level(0, 19, 3.0))
    r = _post_raw(client, "/api/tide/corrections",
                  '{"corrections": {"5": 1e308, "6": -2.5, "7": 0}}')
    assert r.status_code == 201, r.text
    rev2 = r.json()["revision"]
    g = client.get(f"/api/tide/revisions/{rev2}",
                   params={"from_minute": 5, "to_minute": 7}).json()
    assert g["levels"] == {"5": 1e308, "6": -2.5, "7": 0.0}


# ---------------------------------------------------------------------------
# 2. 写路径：误差修订拒绝非有限值与负值（422 VALIDATION_ERROR，整次不生成修订）
# ---------------------------------------------------------------------------

def test_error_revision_rejects_non_finite(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    r = _post_raw(client, "/api/tide/error-revisions", '{"errors": {"3": 1e309}}')
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "VALIDATION_ERROR"
    assert err["details"]["bad_errors"] == [{"minute": 3, "error": "Infinity"}]

    r = _post_raw(client, "/api/tide/error-revisions", '{"errors": {"3": NaN}}')
    assert r.status_code == 422
    assert r.json()["error"]["details"]["bad_errors"] == [
        {"minute": 3, "error": "NaN"}
    ]
    assert _revision_ids(client) == [rev1]


def test_error_revision_rejects_negative_and_non_finite_together(client):
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    r = _post_raw(client, "/api/tide/error-revisions",
                  '{"errors": {"3": -0.5, "4": 1e309, "5": 0.2}}')
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
    assert r.json()["error"]["details"]["bad_errors"] == [
        {"minute": 3, "error": -0.5},
        {"minute": 4, "error": "Infinity"},
    ]
    assert _revision_ids(client) == [rev1]


# ---------------------------------------------------------------------------
# 3. 读路径：查询命中历史异常分钟 -> 422 INVALID_TIDE_DATA（附修订号与分钟）
# ---------------------------------------------------------------------------

def test_query_revision_with_non_finite_values(client):
    rev_ok = seed_initial_revision(_level(0, 19, 3.0))
    bad = {m: (0.5, 0.0) for m in range(20)}
    bad[5] = (INF, 0.0)          # 名义潮位 Infinity
    bad[7] = (0.5, INF)          # 误差 Infinity
    rev_bad = seed_anomalous_revision(bad)

    # 命中异常分钟 -> 422，给出修订号与分钟（及异常字段）
    r = client.get(f"/api/tide/revisions/{rev_bad}",
                   params={"from_minute": 0, "to_minute": 19})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_TIDE_DATA"
    assert err["details"]["revision"] == rev_bad
    assert err["details"]["invalid_minutes"] == [5, 7]
    assert err["details"]["total_invalid"] == 2
    assert err["details"]["bad_values"] == [
        {"minute": 5, "fields": {"level": "Infinity"}},
        {"minute": 7, "fields": {"error": "Infinity"}},
    ]

    # 只命中分钟 5 的子区间同样拒绝；不命中异常的区间正常返回
    r = client.get(f"/api/tide/revisions/{rev_bad}",
                   params={"from_minute": 4, "to_minute": 6})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["invalid_minutes"] == [5]
    r = client.get(f"/api/tide/revisions/{rev_bad}",
                   params={"from_minute": 8, "to_minute": 19})
    assert r.status_code == 200
    assert r.json()["count"] == 12

    # 正常修订不受影响；修订列表不受影响
    assert client.get(f"/api/tide/revisions/{rev_ok}",
                      params={"from_minute": 0, "to_minute": 19}
                      ).status_code == 200
    assert _revision_ids(client) == [rev_ok, rev_bad]


def test_startup_remains_usable_with_anomalous_history(client):
    # 库中已有异常修订时，启动（init_db 幂等重跑）与健康检查不受影响
    bad = {m: (0.5, 0.0) for m in range(5)}
    bad[2] = (INF, 0.0)
    seed_anomalous_revision(bad)
    conn = connect()
    init_db(conn)   # 幂等重入，不应报错
    conn.close()
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 4. 读路径：单船规划/确认需要异常分钟 -> 422，绝不按安全处理
# ---------------------------------------------------------------------------

def _seed_route_and_anomaly(client, depth=1.0):
    """A->B 航段 + 全窗口潮位 + 第 5 分钟 Infinity 异常（历史遗留）。

    若异常被按安全处理（Infinity 潮位判为可通航），规划会正常给出行程；
    正确行为是窗口需要该分钟时一律 422 INVALID_TIDE_DATA。
    """
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=depth)
    bad = {m: (0.5, 0.0) for m in range(20)}
    bad[5] = (INF, 0.0)
    return seed_anomalous_revision(bad)


def test_plan_never_treats_infinite_tide_as_navigable(client):
    rev_bad = _seed_route_and_anomaly(client)
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev_bad,
    })
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_TIDE_DATA"
    assert err["details"]["revision"] == rev_bad
    assert err["details"]["invalid_minutes"] == [5]


def test_plan_on_window_without_anomaly_still_works(client):
    rev_bad = _seed_route_and_anomaly(client, depth=99.0)
    # 窗口 [10, 15) 不含异常分钟：正常规划（深 99 全程可通航）
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 10, "window_minutes": 5,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev_bad,
    })
    assert r.status_code == 200, r.text
    assert r.json()["itinerary"][0]["start_minute"] == 10


def test_confirm_rejected_when_revision_has_anomaly(client):
    rev_bad = _seed_route_and_anomaly(client)
    # 直接构造确认请求（规划已 422，无法取得真实行程；复算阶段即应拒绝）
    r = client.post("/api/trips/confirm", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev_bad,
        "itinerary": [],
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_TIDE_DATA"
    assert client.get("/api/trips").json() == []   # 不保存任何快照


# ---------------------------------------------------------------------------
# 5. 读路径：批次预排/确认需要异常分钟 -> 422（附 ship_index），全批不保存
# ---------------------------------------------------------------------------

def _batch_body(rev, window=20):
    ship = {
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": window,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
    }
    return {"ships": [dict(ship), dict(ship)], "revision": rev}


def test_batch_plan_rejected_with_ship_index(client):
    rev_bad = _seed_route_and_anomaly(client)
    r = client.post("/api/trips/batch/plan", json=_batch_body(rev_bad))
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_TIDE_DATA"
    assert err["details"]["ship_index"] == 0
    assert err["details"]["revision"] == rev_bad


def test_batch_confirm_rejected_and_key_reusable(client):
    rev_bad = _seed_route_and_anomaly(client)
    body = _batch_body(rev_bad)
    for s in body["ships"]:
        s["itinerary"] = []
    body["idempotency_key"] = "batch-anomaly-1"
    r = client.post("/api/trips/batch/confirm", json=body)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_TIDE_DATA"
    assert client.get("/api/trips").json() == []   # 全批不保存

    # 被拒绝的请求不登记幂等记录：以有限值覆盖异常分钟生成新修订后，
    # 同一幂等键可用于首次成功提交
    rev2 = client.post(
        "/api/tide/corrections",
        json={"corrections": {5: 3.0}, "base_revision": rev_bad},
    ).json()["revision"]
    planned = client.post("/api/trips/batch/plan", json=_batch_body(rev2))
    assert planned.status_code == 200, planned.text
    plan = planned.json()
    body2 = {
        "ships": [
            {**{k: s[k] for k in ("origin", "destination", "start_minute",
                                  "window_minutes", "draft", "height", "margin")},
             "itinerary": s["itinerary"]}
            for s in plan["ships"]
        ],
        "revision": rev2,
        "idempotency_key": "batch-anomaly-1",
    }
    r = client.post("/api/trips/batch/confirm", json=body2)
    assert r.status_code == 201, r.text
    assert r.json()["replayed"] is False
    assert len(client.get("/api/trips").json()) == 2


# ---------------------------------------------------------------------------
# 6. 读路径：潮位复核与改期确认需要异常分钟 -> 422，不改变状态
# ---------------------------------------------------------------------------

def _confirm_trip_on_good_revision(client):
    """在良好修订上确认一条行程，返回 (trip, 异常修订号)。

    异常修订以确认所用修订为基础、直接写库生成（模拟历史遗留）：第 6 分钟
    名义潮位 Infinity（该分钟位于行程窗口内）。
    """
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=3, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    planned = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev1,
    }).json()
    r = client.post("/api/trips/confirm", json=planned)
    assert r.status_code == 201, r.text
    trip = r.json()
    # 基于 rev1 的差量异常修订（历史遗留）：分钟 6 潮位 Infinity
    rev_bad = seed_anomalous_revision({6: (INF, 0.0)}, base=rev1)
    return trip, rev_bad


def test_review_rejected_when_target_revision_has_anomaly(client):
    trip, rev_bad = _confirm_trip_on_good_revision(client)
    r = client.post(f"/api/trips/{trip['trip_id']}/review",
                    json={"target_revision": rev_bad})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "INVALID_TIDE_DATA"
    assert err["details"]["revision"] == rev_bad
    assert err["details"]["invalid_minutes"] == [6]
    # 快照不改写
    assert client.get(f"/api/trips/{trip['trip_id']}").json()[
        "revision"] == trip["revision"]


def test_reschedule_confirm_rejected_and_state_unchanged(client):
    trip, rev_bad = _confirm_trip_on_good_revision(client)
    r = client.post(f"/api/trips/{trip['trip_id']}/reschedule", json={
        "target_revision": rev_bad,
        "departure_minutes": [0],
        "idempotency_key": "rs-anomaly-1",
    })
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_TIDE_DATA"
    # 不改变状态：原行程未作废、无新行程、幂等键未登记
    assert client.get(f"/api/trips/{trip['trip_id']}").json()[
        "voided_at"] is None
    assert len(client.get("/api/trips").json()) == 1
    r = client.post(f"/api/trips/{trip['trip_id']}/reschedule", json={
        "target_revision": rev_bad,
        "departure_minutes": [0],
        "idempotency_key": "rs-anomaly-1",
    })
    assert r.status_code == 422   # 同键重试仍按首次校验拒绝（非重放）


# ---------------------------------------------------------------------------
# 7. 分叉恢复：异常字段被有限新值覆盖才恢复；其余字段/分钟按原规则继承
# ---------------------------------------------------------------------------

def test_fork_recovers_only_overwritten_field(client):
    # 历史异常修订：分钟 5 潮位 Infinity；分钟 7 误差 Infinity
    bad = {m: (0.5, 0.0) for m in range(20)}
    bad[5] = (INF, 0.0)
    bad[7] = (0.5, INF)
    rev_bad = seed_anomalous_revision(bad)

    # 分叉 1：更正覆盖分钟 5 的潮位为有限值 -> 分钟 5 恢复；分钟 7 仍异常
    r = client.post("/api/tide/corrections",
                    json={"corrections": {5: 2.0}, "base_revision": rev_bad})
    assert r.status_code == 201, r.text
    rev2 = r.json()["revision"]
    r = client.get(f"/api/tide/revisions/{rev2}",
                   params={"from_minute": 5, "to_minute": 5})
    assert r.status_code == 200
    assert r.json()["levels"] == {"5": 2.0}
    assert r.json()["errors"] == {"5": 0.0}
    r = client.get(f"/api/tide/revisions/{rev2}",
                   params={"from_minute": 0, "to_minute": 19})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["invalid_minutes"] == [7]

    # 分叉 2：误差修订覆盖分钟 7 的误差为有限值 -> 分钟 7 恢复；分钟 5 仍异常
    r = client.post("/api/tide/error-revisions",
                    json={"errors": {7: 0.3}, "base_revision": rev_bad})
    assert r.status_code == 201, r.text
    rev3 = r.json()["revision"]
    r = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 7, "to_minute": 7})
    assert r.status_code == 200
    assert r.json()["errors"] == {"7": 0.3}
    r = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 0, "to_minute": 19})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["invalid_minutes"] == [5]

    # 原始异常修订不改写：仍全部异常
    r = client.get(f"/api/tide/revisions/{rev_bad}",
                   params={"from_minute": 0, "to_minute": 19})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["invalid_minutes"] == [5, 7]


def test_fork_correction_inherits_anomalous_error_field(client):
    # 分钟 9 潮位与误差均异常：更正只覆盖潮位 -> 误差异常仍继承，分钟未恢复
    bad = {m: (0.5, 0.0) for m in range(20)}
    bad[9] = (INF, INF)
    rev_bad = seed_anomalous_revision(bad)

    r = client.post("/api/tide/corrections",
                    json={"corrections": {9: 2.0}, "base_revision": rev_bad})
    rev2 = r.json()["revision"]
    r = client.get(f"/api/tide/revisions/{rev2}",
                   params={"from_minute": 9, "to_minute": 9})
    assert r.status_code == 422
    assert r.json()["error"]["details"]["bad_values"] == [
        {"minute": 9, "fields": {"error": "Infinity"}}
    ]

    # 再以误差修订覆盖该分钟误差 -> 该分钟完全恢复
    r = client.post("/api/tide/error-revisions",
                    json={"errors": {9: 0.4}, "base_revision": rev2})
    assert r.status_code == 201, r.text
    rev3 = r.json()["revision"]
    r = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 0, "to_minute": 19})
    assert r.status_code == 200
    assert r.json()["levels"]["9"] == 2.0
    assert r.json()["errors"]["9"] == 0.4


def test_plan_recovers_after_anomalous_minute_overwritten(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    bad = {m: (3.0, 0.0) for m in range(20)}
    bad[5] = (INF, 0.0)
    rev_bad = seed_anomalous_revision(bad)

    # 异常分钟在窗口内：规划拒绝
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev_bad,
    })
    assert r.status_code == 422

    # 有限值覆盖异常分钟后分叉：同一窗口规划恢复
    rev2 = client.post("/api/tide/corrections",
                       json={"corrections": {5: 3.0},
                             "base_revision": rev_bad}).json()["revision"]
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 1.0, "height": 0.0, "margin": 0.0,
        "revision": rev2,
    })
    assert r.status_code == 200, r.text
    assert r.json()["itinerary"][0]["start_minute"] == 0


# ---------------------------------------------------------------------------
# 8. 有效数据行为保持兼容：有限值修订的继承与误差带判定不变
# ---------------------------------------------------------------------------

def test_valid_revisions_unaffected(client):
    add_node(client, "A"); add_node(client, "B")
    add_segment(client, "A", "B", travel_time=2, depth=4.0)
    rev1 = seed_initial_revision(_level(0, 19, 3.0))
    rev2 = client.post("/api/tide/error-revisions",
                       json={"errors": {0: 0.5}}).json()["revision"]
    rev3 = client.post("/api/tide/corrections",
                       json={"corrections": {1: 2.0}}).json()["revision"]
    g = client.get(f"/api/tide/revisions/{rev3}",
                   params={"from_minute": 0, "to_minute": 2}).json()
    assert g["levels"] == {"0": 3.0, "1": 2.0, "2": 3.0}
    assert g["errors"] == {"0": 0.5, "1": 0.0, "2": 0.0}
    # 误差带判定不变：分钟 0 可用水深 4+3.0-0.5=6.5、分钟 1 为 4+2.0-0=6.0，
    # 均小于吃水 6.6；分钟 2 起 4+3.0-0=7.0 可行
    r = client.post("/api/trips/plan", json={
        "origin": "A", "destination": "B",
        "start_minute": 0, "window_minutes": 20,
        "draft": 6.6, "height": 0.0, "margin": 0.0,
        "revision": rev3,
    })
    assert r.status_code == 200
    assert r.json()["itinerary"][0]["start_minute"] == 2
    assert _revision_ids(client) == [rev1, rev2, rev3]
