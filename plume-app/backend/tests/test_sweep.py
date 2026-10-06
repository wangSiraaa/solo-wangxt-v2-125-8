"""稳定度扫描端点验收测试。

对应验收标准：
1. 同一距离可逐列核对到单点求值（/api/plume/points 完全一致）；
2. 适用范围外的点被标注（in_valid_range=False + flags），数值仍返回
   但响应显式声明"仅作趋势演示"，不伪装为精确预测；
3. 静风或非法距离使试算整体失败（422），响应中无任何部分结果；
4. 扫描为独立试算：已保存气象情景在扫描前后完全一致。
"""
from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from app.geometry import local_to_lonlat, transport_bearing_deg
from app.main import app

client = TestClient(app)

LON0, LAT0 = 116.40, 39.90
WIND_FROM = 270.0  # 西风 -> 烟羽向东


def receptors_from_distances(distances, wind_from=WIND_FROM):
    """把下风向距离（m）换算为真实坐标受体（与前端同一等距圆柱近似）。"""
    theta = math.radians(transport_bearing_deg(wind_from))
    pts = []
    for d in distances:
        e = d * math.sin(theta)
        n = d * math.cos(theta)
        lon, lat = local_to_lonlat(e, n, LON0, LAT0)
        pts.append([lon, lat])
    return pts


def base_payload(distances=(200.0, 500.0, 1000.0, 2000.0, 5000.0), wind_speed=6.0, **kw):
    payload = {
        "source": {
            "name": "扫描测试源", "lon": LON0, "lat": LAT0,
            "stack_height_m": 120.0, "emission_rate_g_s": 50.0,
            "stack_diameter_m": 4.0, "exit_velocity_ms": 18.0,
            "stack_temp_k": 410.0, "pollutant": "SO2",
        },
        "meteorology": {
            "name": "扫描固定风", "wind_from_deg": WIND_FROM,
            "wind_speed_ms": wind_speed, "stability_class": "D",
            "ambient_temp_k": 293.15, "pressure_hpa": 1013.0,
            "background_conc_ug_m3": 15.0,
        },
        "receptors": receptors_from_distances(distances),
        "calm_threshold_ms": 1.0,
    }
    payload.update(kw)
    return payload


def test_sweep_columns_match_single_point_evaluation():
    """验收 1：同一距离处，扫描表的每一列都与单点求值完全一致。"""
    payload = base_payload()
    resp = client.post("/api/plume/stability-sweep", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [s["stability"] for s in data["results"]] == list("ABCDEF")
    points = [r["lonlat"] for r in data["receptors"]]

    for stab_result in data["results"]:
        cls = stab_result["stability"]
        met = dict(payload["meteorology"])
        met["stability_class"] = cls
        preq = {
            "source": payload["source"],
            "meteorology": met,
            "points": points,
            "calm_threshold_ms": payload["calm_threshold_ms"],
        }
        presp = client.post("/api/plume/points", json=preq)
        assert presp.status_code == 200
        pts = presp.json()["points"]
        assert len(pts) == len(stab_result["rows"])
        for row, p in zip(stab_result["rows"], pts):
            # 逐列精确一致（同一模型入口，逐元素位级相同）
            assert row["downwind_x_m"] == p["downwind_crosswind_m"][0]
            assert row["plume_ug_m3"] == p["plume_conc_ug_m3"]
            assert row["background_ug_m3"] == p["background_conc_ug_m3"]
            assert row["total_ug_m3"] == p["total_conc_ug_m3"]
            assert row["sigma_y_m"] == p["sigma_y_m"]
            assert row["sigma_z_m"] == p["sigma_z_m"]


def test_sweep_calm_wind_fails_without_partial_results():
    """验收 3a：静风 -> 422 calm_wind，响应不含任何部分结果。"""
    payload = base_payload(wind_speed=0.3)
    resp = client.post("/api/plume/stability-sweep", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "calm_wind"
    for key in ("results", "receptors", "rows", "stability_classes"):
        assert key not in body


def test_sweep_invalid_distance_fails_without_partial_results():
    """验收 3b：上风向/源点受体 -> 422 invalid_input，无部分结果。"""
    # 源正西 1 km：对西风（向东输运）而言是上风向
    upwind_lon, upwind_lat = local_to_lonlat(-1000.0, 0.0, LON0, LAT0)

    payload = base_payload()
    payload["receptors"] = receptors_from_distances([500.0, 1000.0]) + [
        [upwind_lon, upwind_lat]
    ]
    resp = client.post("/api/plume/stability-sweep", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"] == "invalid_input"
    assert "#3" in body["message"]  # 指出非法受体序号
    for key in ("results", "receptors", "rows"):
        assert key not in body

    # 受体恰好位于源点（x = 0）同样非法
    payload2 = base_payload()
    payload2["receptors"] = [[LON0, LAT0]]
    resp2 = client.post("/api/plume/stability-sweep", json=payload2)
    assert resp2.status_code == 422
    assert resp2.json()["error"] == "invalid_input"
    assert "results" not in resp2.json()


def test_sweep_out_of_range_flagged_not_hidden():
    """验收 2：适用范围外的点被标注，数值仍返回但不伪装为精确预测。"""
    payload = base_payload(distances=(50.0, 500.0, 20_000.0, 40_000.0))
    resp = client.post("/api/plume/stability-sweep", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    recs = data["receptors"]
    # 50 m：近源 + 超出 Briggs 建议范围
    assert recs[0]["in_valid_range"] is False
    assert "NEAR_SOURCE" in recs[0]["flags"]
    assert "OUT_OF_BRIGGS_RANGE" in recs[0]["flags"]
    # 500 m：范围内，无标记
    assert recs[1]["in_valid_range"] is True
    assert recs[1]["flags"] == []
    # 20 km：超出 Briggs 10 km 上限
    assert recs[2]["in_valid_range"] is False
    assert "OUT_OF_BRIGGS_RANGE" in recs[2]["flags"]
    # 40 km：另加平面近似尺度提示
    assert "FLAT_EARTH_ADVISORY" in recs[3]["flags"]
    # 数值仍给出（有限、非负），但响应显式声明适用范围与免责
    for stab in data["results"]:
        for row in stab["rows"]:
            assert math.isfinite(row["total_ug_m3"])
            assert row["total_ug_m3"] >= 0.0
            assert row["flags"] == recs[row["receptor_index"]]["flags"]
    assert data["parameterization"]["valid_range_m"] == [100.0, 10_000.0]
    assert "趋势" in data["parameterization"]["range_note"]
    assert "精确预测" in data["parameterization"]["range_note"]
    assert data["flag_legend"]["OUT_OF_BRIGGS_RANGE"]
    assert "教学" in data["disclaimer"]


def test_sweep_peak_analytic_power_law():
    """线性幂律下峰值位置解析解 x_peak = H/(√2·az)，各稳定度一致。"""
    payload = base_payload(distances=(100.0, 300.0, 1000.0, 3000.0))
    payload["parameterization"] = "power_law"
    payload["power_law"] = {"ay": 0.22, "py": 1.0, "az": 0.16, "pz": 1.0}
    payload["source"]["stack_height_m"] = 60.0
    resp = client.post("/api/plume/stability-sweep", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    expected = 60.0 / (math.sqrt(2.0) * 0.16)
    for stab in data["results"]:
        peak_x = stab["peak"]["downwind_x_m"]
        assert abs(peak_x - expected) / expected < 0.01
        # 幂律：每行都标注为教学核对参数化，in_valid_range 无定义
        assert all("TEACHING_PARAMETERIZATION" in r["flags"] for r in stab["rows"])
        assert all(r["in_valid_range"] is None for r in stab["rows"])
    assert data["parameterization"]["valid_range_m"] is None
    assert data["parameterization"]["coefficients"]["az"] == 0.16


def test_sweep_briggs_peak_ordering_and_refinement():
    """Briggs 下不稳定类峰值更近；求精峰值不低于受体采样最大值。"""
    payload = base_payload(distances=(100.0, 500.0, 1000.0, 2000.0, 5000.0, 10_000.0))
    resp = client.post("/api/plume/stability-sweep", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    peaks = {s["stability"]: s["peak"] for s in data["results"]}
    assert peaks["A"]["downwind_x_m"] < peaks["F"]["downwind_x_m"]
    for stab in data["results"]:
        sampled_max = max(r["total_ug_m3"] for r in stab["rows"])
        assert stab["peak"]["total_ug_m3"] >= sampled_max - 1e-9
        assert stab["sampled_peak"]["total_ug_m3"] == pytest.approx(sampled_max)
        assert "PEAK_AT_SCAN_EDGE" not in stab["peak"]["flags"]


def test_sweep_subset_classes_and_background_identity():
    """稳定度子集（去重保序）；total = plume + background 逐行成立。"""
    payload = base_payload()
    payload["stability_classes"] = ["F", "D", "F"]
    resp = client.post("/api/plume/stability-sweep", json=payload)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert [s["stability"] for s in data["results"]] == ["F", "D"]
    for stab in data["results"]:
        for row in stab["rows"]:
            assert row["total_ug_m3"] == pytest.approx(
                row["plume_ug_m3"] + row["background_ug_m3"]
            )
            assert row["background_ug_m3"] == 15.0


def test_sweep_does_not_modify_saved_scenarios():
    """验收 4：扫描是独立试算，已保存气象情景不被修改。"""
    before = client.get("/api/meteorology").json()
    resp = client.post("/api/plume/stability-sweep", json=base_payload())
    assert resp.status_code == 200
    after = client.get("/api/meteorology").json()
    assert before == after
    assert "独立试算" in resp.json()["trial_note"]
