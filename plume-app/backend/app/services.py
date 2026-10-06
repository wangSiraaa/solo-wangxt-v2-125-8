"""把模型组装为可复用的计算服务（网格与单点）。"""
from __future__ import annotations

import math

import numpy as np

from .config import settings
from .dispersion import (
    STABILITY_CLASSES,
    STABILITY_DESCRIPTIONS,
    parameterization_metadata,
)
from .gaussian import (
    CalmWindError,
    PlumeInputError,
    compute_plume_field,
    ensure_wind_speed_ok,
)
from .geometry import (
    local_to_lonlat,
    local_to_plume_coords,
    lonlat_to_local,
    transport_bearing_deg,
    wind_transform_check,
)
from .plume_rise import holland_plume_rise
from .schemas import (
    GridSpec,
    MeteorologyInput,
    PlumeGridRequest,
    PlumePointRequest,
    SourceInput,
    StabilitySweepRequest,
)

DISCLAIMER = (
    "教学演示结果：基于平坦地形、稳态风、定常排放的解析高斯烟羽模型，"
    "不含地形、建筑物下洗、沉降与化学反应。不得用于真实事故预警或法规达标判定。"
)


def merge_overrides(req: PlumeGridRequest) -> tuple[SourceInput, MeteorologyInput]:
    """把临时 override 合并到源/气象副本上；原始输入对象不变。"""
    src = req.source.model_copy(deep=True)
    met = req.meteorology.model_copy(deep=True)
    if req.source_override is not None:
        for key, val in req.source_override.model_dump(exclude_none=True).items():
            setattr(src, key, val)
    if req.met_override is not None:
        for key, val in req.met_override.model_dump(exclude_none=True).items():
            setattr(met, key, val)
    return src, met


def effective_height(src: SourceInput, met: MeteorologyInput, use_rise: bool) -> tuple[float, dict]:
    """计算有效源高；返回 (H_e, 抬升明细)。"""
    if not use_rise:
        return src.stack_height_m, {"delta_h_m": 0.0, "used": False}
    detail = holland_plume_rise(
        exit_velocity_ms=src.exit_velocity_ms,
        stack_diameter_m=src.stack_diameter_m,
        wind_speed_ms=met.wind_speed_ms,
        stack_temp_k=src.stack_temp_k,
        ambient_temp_k=met.ambient_temp_k,
        pressure_hpa=met.pressure_hpa,
    )
    detail["used"] = True
    return src.stack_height_m + detail["delta_h_m"], detail


def build_sampling_grid(spec: GridSpec, wind_from_deg: float, lon0: float, lat0: float) -> dict:
    """在烟羽坐标 (x 下风向, y 横风向) 上建采样矩形，再转回经纬度。

    返回 E/N 二维网格、角点（经纬度+米）和间距。分辨率只影响采样，
    不触碰任何源/气象输入。
    """
    x = np.linspace(-spec.upwind_extent_m, spec.downwind_extent_m, spec.nx)
    y = np.linspace(-spec.crosswind_extent_m / 2.0, spec.crosswind_extent_m / 2.0, spec.ny)
    xx, yy = np.meshgrid(x, y)  # 行=y, 列=x

    theta = math.radians(transport_bearing_deg(wind_from_deg))
    # 烟羽坐标 -> 局部 E/N（local_to_plume_coords 的逆）
    east = xx * math.sin(theta) + yy * math.cos(theta)
    north = xx * math.cos(theta) - yy * math.sin(theta)

    lon_grid, lat_grid = local_to_lonlat(east, north, lon0, lat0)

    corners = []
    for (cx, cy) in [
        (x[0], y[0]),
        (x[-1], y[0]),
        (x[-1], y[-1]),
        (x[0], y[-1]),
    ]:
        ce = cx * math.sin(theta) + cy * math.cos(theta)
        cn = cx * math.cos(theta) - cy * math.sin(theta)
        clon, clat = local_to_lonlat(ce, cn, lon0, lat0)
        corners.append(
            {
                "plume_x_y_m": [float(cx), float(cy)],
                "east_north_m": [float(ce), float(cn)],
                "lonlat": [float(clon), float(clat)],
            }
        )

    dx = (spec.downwind_extent_m + spec.upwind_extent_m) / (spec.nx - 1)
    dy = spec.crosswind_extent_m / (spec.ny - 1)
    flat_scale = max(
        spec.downwind_extent_m + spec.upwind_extent_m,
        spec.crosswind_extent_m,
    )
    return {
        "x_edges_m": x.tolist(),
        "y_edges_m": y.tolist(),
        "east_m": east,
        "north_m": north,
        "lon_grid": lon_grid,
        "lat_grid": lat_grid,
        "corners": corners,
        "nx": spec.nx,
        "ny": spec.ny,
        "spacing_downwind_m": float(dx),
        "spacing_crosswind_m": float(dy),
        "flat_earth_scale_m": float(flat_scale),
        "flat_earth_advisory_m": settings.flat_earth_advisory_m,
        "flat_earth_warning": flat_scale > settings.flat_earth_advisory_m,
    }


def iso_levels(max_value: float, n_levels: int = 8) -> list[float]:
    """以 1-2-5 ×10^k 生成友好等值级，始终包含 0 以上的正级；
    最大浓度为零时返回空列表。"""
    if max_value <= 0 or not np.isfinite(max_value):
        return []
    hi_exp = math.floor(math.log10(max_value))
    mantissas = [1.0, 2.0, 5.0]
    levels: list[float] = []
    for e in range(hi_exp - 4, hi_exp + 1):
        for m in mantissas:
            v = m * 10.0**e
            if v < max_value:
                levels.append(v)
    # 去重保序，控制数量
    out: list[float] = []
    for v in levels:
        if not out or abs(v - out[-1]) / v > 1e-9:
            out.append(v)
    return out[-n_levels:]


def run_grid(req: PlumeGridRequest) -> dict:
    """完整的网格计算流程；静风/非法输入向上抛 CalmWindError/PlumeInputError。"""
    spec = req.grid
    if (
        (spec.downwind_extent_m + spec.upwind_extent_m) / max(spec.nx - 1, 1)
        < settings.grid_min_spacing_m
        or spec.crosswind_extent_m / max(spec.ny - 1, 1)
        < settings.grid_min_spacing_m
    ):
        raise PlumeInputError(
            f"采样间距不能小于 {settings.grid_min_spacing_m:g} m"
        )

    src, met = merge_overrides(req)
    h_eff, rise_detail = effective_height(src, met, req.plume_rise.use_plume_rise)
    grid = build_sampling_grid(spec, met.wind_from_deg, src.lon, src.lat)

    result = compute_plume_field(
        emission_rate_g_s=src.emission_rate_g_s,
        wind_speed_ms=met.wind_speed_ms,
        wind_from_deg=met.wind_from_deg,
        stability_class=met.stability_class,
        effective_height_m=h_eff,
        east_m=grid["east_m"],
        north_m=grid["north_m"],
        parameterization=req.parameterization,
        power_law=req.power_law,
        calm_threshold_ms=req.calm_threshold_ms,
    )
    plume = result["field"]
    bg = met.background_conc_ug_m3
    total = plume + bg

    return {
        "source_lonlat": [src.lon, src.lat],
        "crs_note": (
            "经纬度 EPSG:4326；内部为以源为原点的局部等距圆柱平面(米)，"
            "小尺度平坦地形近似，建议采样尺度 ≤ 30 km"
        ),
        "grid": {
            "nx": grid["nx"],
            "ny": grid["ny"],
            "x_edges_m": grid["x_edges_m"],
            "y_edges_m": grid["y_edges_m"],
            "lon_grid": np.asarray(grid["lon_grid"]).tolist(),
            "lat_grid": np.asarray(grid["lat_grid"]).tolist(),
            "spacing_downwind_m": grid["spacing_downwind_m"],
            "spacing_crosswind_m": grid["spacing_crosswind_m"],
            "corners_lonlat": [c["lonlat"] for c in grid["corners"]],
            "sampling_extent_lonlat": {
                "lon_min": float(np.min(grid["lon_grid"])),
                "lon_max": float(np.max(grid["lon_grid"])),
                "lat_min": float(np.min(grid["lat_grid"])),
                "lat_max": float(np.max(grid["lat_grid"])),
            },
            "flat_earth_warning": grid["flat_earth_warning"],
            "resolution_disclaimer": (
                "等值面为有限采样网格上的估计值，网格间距 "
                f"{grid['spacing_downwind_m']:.0f}(下风向)/"
                f"{grid['spacing_crosswind_m']:.0f}(横风向) m；"
                "不代表网格之外或亚网格尺度的浓度"
            ),
        },
        "plume_field_ug_m3": plume.tolist(),
        "background_conc_ug_m3": float(bg),
        "total_conc_ug_m3": total.tolist(),
        "iso_levels_ug_m3": iso_levels(float(plume.max())),
        "effective_stack_height_m": float(h_eff),
        "plume_rise_delta_h_m": float(rise_detail["delta_h_m"]),
        "wind": {
            **wind_transform_check(met.wind_from_deg),
            "wind_speed_ms": met.wind_speed_ms,
            "stability_class": met.stability_class,
        },
        "source_term": {
            "name": src.name,
            "pollutant": src.pollutant,
            "emission_rate_g_s": src.emission_rate_g_s,
            "stack_height_m": src.stack_height_m,
            "stack_diameter_m": src.stack_diameter_m,
            "exit_velocity_ms": src.exit_velocity_ms,
            "stack_temp_k": src.stack_temp_k,
            "plume_rise_detail": rise_detail,
        },
        "diagnostics": result["diagnostics"],
        "validity": {
            "model": "steady-state Gaussian plume, flat terrain, full ground reflection",
            "assumptions": [
                "定常排放、稳态风、平坦均一下垫面",
                "污染物守恒（无沉降、无化学转化、无建筑物下洗）",
                "浓度在横风向与垂直方向服从高斯分布",
            ],
            "parameterizations": parameterization_metadata(),
            "briggs_valid_range_m": [
                settings.briggs_valid_x_min_m,
                settings.briggs_valid_x_max_m,
            ],
            "stability_classes": list(STABILITY_CLASSES),
        },
        "disclaimer": DISCLAIMER,
    }


def run_points(req: "PlumePointRequest") -> list[dict]:
    """在任意经纬度点上求值（核对用），与采样网格完全无关。"""
    src, met = merge_overrides(req)
    h_eff, rise_detail = effective_height(src, met, req.plume_rise.use_plume_rise)
    out = []
    for lon, lat in req.points:
        e, n = lonlat_to_local(lon, lat, src.lon, src.lat)
        ea = np.array([[e]])
        na = np.array([[n]])
        result = compute_plume_field(
            emission_rate_g_s=src.emission_rate_g_s,
            wind_speed_ms=met.wind_speed_ms,
            wind_from_deg=met.wind_from_deg,
            stability_class=met.stability_class,
            effective_height_m=h_eff,
            east_m=ea,
            north_m=na,
            parameterization=req.parameterization,
            power_law=req.power_law,
            calm_threshold_ms=req.calm_threshold_ms,
        )
        x = float(result["x_downwind_m"][0, 0])
        y = float(result["y_crosswind_m"][0, 0])
        plume = float(result["field"][0, 0])
        out.append(
            {
                "lonlat": [lon, lat],
                "east_north_m": [e, n],
                "downwind_crosswind_m": [x, y],
                "plume_conc_ug_m3": plume,
                "background_conc_ug_m3": met.background_conc_ug_m3,
                "total_conc_ug_m3": plume + met.background_conc_ug_m3,
                "sigma_y_m": float(result["sigma_y_m"][0, 0]),
                "sigma_z_m": float(result["sigma_z_m"][0, 0]),
            }
        )
    return out


# 稳定度扫描的受体距离上限与采样网格半宽一致（局部平面近似适用范围）
SWEEP_MAX_DISTANCE_M = settings.grid_max_half_extent_m


def _receptor_range_flag(
    distance_m: float, parameterization: str
) -> tuple[bool | None, str]:
    """受体距离的适用范围标记。

    Briggs 乡村系数建议 x ∈ [100 m, 10 km]；范围外照常计算但显式标注，
    仅作趋势演示。幂律为解析核对方案，无官方适用范围，标记为 None。
    """
    if parameterization == "briggs_rural":
        lo = settings.briggs_valid_x_min_m
        hi = settings.briggs_valid_x_max_m
        if distance_m < lo:
            return False, "below_briggs_valid_range"
        if distance_m > hi:
            return False, "above_briggs_valid_range"
        return True, "within_briggs_valid_range"
    return None, "no_official_valid_range"


def run_stability_sweep(req: StabilitySweepRequest) -> dict:
    """稳定度扫描：固定源项/风速/风向，逐稳定度在一组下风向受体上求值。

    设计约束：
    * 独立试算——只用请求中源/气象的副本，不读取、不写回任何已保存情景；
    * 全部校验（距离合法性、静风）在任何浓度计算之前完成，
      失败即整体 422，不产生部分结果、不保存任何记录；
    * 每个 (稳定度, 受体) 的浓度通过 run_points 求值——与
      POST /api/plume/points 单点求值完全同一代码路径，可逐列核对。
    """
    # ---- 1) 受体距离校验：任一非法即整体失败 ----
    distances: list[float] = []
    for d in req.receptor_distances_m:
        if not math.isfinite(d):
            raise PlumeInputError(f"受体距离含非有限值: {d!r}")
        if d <= 0.0:
            raise PlumeInputError(
                f"受体距离必须为正的下风向距离，收到 {d:g} m"
            )
        if d > SWEEP_MAX_DISTANCE_M:
            raise PlumeInputError(
                f"受体距离 {d:g} m 超出本模型平面近似允许的上限 "
                f"{SWEEP_MAX_DISTANCE_M:g} m"
            )
        distances.append(float(d))

    src, met = merge_overrides(req)
    # ---- 2) 静风前置拦截（与 compute_plume_field 同一校验函数）----
    ensure_wind_speed_ok(met.wind_speed_ms, req.calm_threshold_ms)

    # ---- 3) 受体：下风向距离 -> 真实经纬度（沿输运方位角）----
    theta = math.radians(transport_bearing_deg(met.wind_from_deg))
    receptors: list[dict] = []
    for d in distances:
        e = d * math.sin(theta)
        n = d * math.cos(theta)
        lon, lat = local_to_lonlat(e, n, src.lon, src.lat)
        receptors.append(
            {
                "distance_m": d,
                "lonlat": [float(lon), float(lat)],
                "east_north_m": [float(e), float(n)],
            }
        )

    h_eff, rise_detail = effective_height(src, met, req.plume_rise.use_plume_rise)
    classes = list(req.stability_classes) if req.stability_classes else list(STABILITY_CLASSES)

    # ---- 4) 逐稳定度求值（复用单点求值路径，保证可逐列核对）----
    results: list[dict] = []
    for stab in classes:
        met_s = met.model_copy(update={"stability_class": stab})
        point_req = PlumePointRequest(
            source=src,
            meteorology=met_s,
            points=[tuple(r["lonlat"]) for r in receptors],
            plume_rise=req.plume_rise,
            parameterization=req.parameterization,
            power_law=req.power_law,
            calm_threshold_ms=req.calm_threshold_ms,
        )
        pts = run_points(point_req)
        points_out: list[dict] = []
        for rec, p in zip(receptors, pts):
            in_range, flag = _receptor_range_flag(
                rec["distance_m"], req.parameterization
            )
            points_out.append(
                {
                    "distance_m": rec["distance_m"],
                    "plume_conc_ug_m3": p["plume_conc_ug_m3"],
                    "background_conc_ug_m3": p["background_conc_ug_m3"],
                    "total_conc_ug_m3": p["total_conc_ug_m3"],
                    "sigma_y_m": p["sigma_y_m"],
                    "sigma_z_m": p["sigma_z_m"],
                    "within_valid_range": in_range,
                    "range_flag": flag,
                }
            )
        peak = max(points_out, key=lambda p: p["total_conc_ug_m3"])
        results.append(
            {
                "stability_class": stab,
                "stability_cn": STABILITY_DESCRIPTIONS[stab]["name_cn"],
                "points": points_out,
                "peak_on_receptors": {
                    "distance_m": peak["distance_m"],
                    "plume_conc_ug_m3": peak["plume_conc_ug_m3"],
                    "total_conc_ug_m3": peak["total_conc_ug_m3"],
                    "sampled_on_receptors": True,
                    "note": "所给受体点中的最大值，非连续峰值的解析解",
                },
            }
        )

    if req.parameterization == "briggs_rural":
        coefficients: dict = {
            "parameterization": "briggs_rural",
            "per_stability": parameterization_metadata(),
        }
        valid_range = [
            settings.briggs_valid_x_min_m,
            settings.briggs_valid_x_max_m,
        ]
    else:
        pl = req.power_law or {}
        coefficients = {
            "parameterization": "power_law",
            "formula": "sigma_y = ay*x^py; sigma_z = az*x^pz",
            "coeffs": {
                "ay": pl.get("ay", 0.22),
                "py": pl.get("py", 1.0),
                "az": pl.get("az", 0.16),
                "pz": pl.get("pz", 1.0),
            },
        }
        valid_range = None

    return {
        "source_lonlat": [src.lon, src.lat],
        "wind": {
            **wind_transform_check(met.wind_from_deg),
            "wind_speed_ms": met.wind_speed_ms,
        },
        "receptors": receptors,
        "stability_classes": classes,
        "background_conc_ug_m3": met.background_conc_ug_m3,
        "effective_stack_height_m": float(h_eff),
        "plume_rise_delta_h_m": float(rise_detail["delta_h_m"]),
        "results": results,
        "units": {
            "distance": "m",
            "concentration": "μg/m³",
            "sigma": "m",
            "emission_rate": "g/s",
            "wind_speed": "m/s",
            "conversion": "g → μg 系数 1e6",
        },
        "coefficients": coefficients,
        "validity": {
            "model": "steady-state Gaussian plume, flat terrain, full ground reflection",
            "briggs_valid_range_m": valid_range,
            "out_of_range_policy": (
                "适用范围外的受体照常返回数值并以 range_flag 显式标注，"
                "仅作趋势演示，不作为精确预测"
            ),
            "peak_note": (
                "peak_on_receptors 为所给受体点中的最大值（采样峰值），"
                "不是连续峰值的解析位置"
            ),
            "evaluation_path": (
                "与 POST /api/plume/points 相同的求值代码路径，"
                "同一受体经纬度、同一稳定度下二者逐列一致"
            ),
        },
        "trial_note": (
            "独立试算：仅使用请求中源/气象的副本逐稳定度求值，"
            "不读取、不写回任何已保存气象情景，不产生保存记录。"
        ),
        "disclaimer": DISCLAIMER,
    }
