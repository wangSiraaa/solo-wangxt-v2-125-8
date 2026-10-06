"""把模型组装为可复用的计算服务（网格与单点）。"""
from __future__ import annotations

import math

import numpy as np

from .config import settings
from .dispersion import (
    DEFAULT_POWER_LAW,
    STABILITY_CLASSES,
    STABILITY_DESCRIPTIONS,
    PowerLawParams,
    parameterization_metadata,
)
from .gaussian import CalmWindError, PlumeInputError, compute_plume_field
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


# ---------------------------------------------------------------------------
# 稳定度扫描（独立试算）
# ---------------------------------------------------------------------------

# 适用范围标记词汇表（在响应 flag_legend 中同步给出中文解释）
FLAG_NEAR_SOURCE = "NEAR_SOURCE"
FLAG_OUT_OF_RANGE = "OUT_OF_BRIGGS_RANGE"
FLAG_FLAT_EARTH = "FLAT_EARTH_ADVISORY"
FLAG_TEACHING_PL = "TEACHING_PARAMETERIZATION"
FLAG_PEAK_EDGE = "PEAK_AT_SCAN_EDGE"

FLAG_LEGEND = {
    FLAG_NEAR_SOURCE: (
        f"近源点（x < {settings.briggs_valid_x_min_m:g} m）："
        "σ 接近数值下限，仅作趋势参考"
    ),
    FLAG_OUT_OF_RANGE: (
        f"超出 Briggs 乡村建议适用范围（{settings.briggs_valid_x_min_m:g} m–"
        f"{settings.briggs_valid_x_max_m / 1000:g} km）：仅作趋势演示，非精确预测"
    ),
    FLAG_FLAT_EARTH: (
        f"超出局部平面近似建议尺度（{settings.flat_earth_advisory_m / 1000:g} km）："
        "坐标换算误差增大"
    ),
    FLAG_TEACHING_PL: "幂律为解析核对用教学参数化：无物理适用范围标定，非普适预测",
    FLAG_PEAK_EDGE: "峰值位于内部扫描区间边界：仅作参考",
}

# 中心线峰值内部扫描区间（对数等距），不限制受体距离本身
_SWEEP_SCAN_LO_M = 1.0
_SWEEP_SCAN_HI_M = 200_000.0
_SWEEP_SCAN_N = 6000


def _sweep_flags(x_m: float, briggs: bool) -> tuple[list[str], bool | None]:
    """单个下风向距离的适用范围标记。

    in_valid_range 仅对 briggs_rural 有定义；幂律参数化无物理适用范围
    标定，返回 None 并整体标记 TEACHING_PARAMETERIZATION。
    边界比较带 1e-9 相对容差：受体经纬度经坐标换算往返后，
    恰好落在边界（如 100 m）上的距离不应因浮点尾差被误标。
    """
    eps = 1e-9
    flags: list[str] = []
    in_range: bool | None = None
    if briggs:
        x_min = settings.briggs_valid_x_min_m
        x_max = settings.briggs_valid_x_max_m
        in_range = x_min * (1.0 - eps) <= x_m <= x_max * (1.0 + eps)
        if x_m < x_min * (1.0 - eps):
            flags.append(FLAG_NEAR_SOURCE)
        if not in_range:
            flags.append(FLAG_OUT_OF_RANGE)
    else:
        flags.append(FLAG_TEACHING_PL)
    if x_m > settings.flat_earth_advisory_m * (1.0 + eps):
        flags.append(FLAG_FLAT_EARTH)
    return flags, in_range


def _golden_max_scalar(func, a: float, b: float, iters: int = 200) -> tuple[float, float]:
    """黄金分割求单峰函数在 [a, b] 上的最大值（中心线峰值求精用）。"""
    gr = (math.sqrt(5.0) - 1.0) / 2.0
    c = b - gr * (b - a)
    d = a + gr * (b - a)
    fc, fd = func(c), func(d)
    for _ in range(iters):
        if abs(b - a) <= 1e-9 * max(1.0, abs(a), abs(b)):
            break
        if fc < fd:
            a, c, fc = c, d, fd
            d = a + gr * (b - a)
            fd = func(d)
        else:
            b, d, fd = d, c, fc
            c = b - gr * (b - a)
            fc = func(c)
    xm = 0.5 * (a + b)
    return xm, func(xm)


def run_stability_sweep(req: StabilitySweepRequest) -> dict:
    """稳定度扫描：固定源项/风速/风向/背景，逐稳定度在真实坐标受体上求值。

    与 /api/plume/points 共用同一个 compute_plume_field 入口，保证同一
    距离处的扫描结果可逐列核对到单点求值。

    独立试算——只消费请求内参数，不读取/不写回已保存气象情景，结果不保存。
    静风或任一受体距离非法（x<=0）→ 整体报错，不返回任何部分结果。
    """
    src, met = merge_overrides(req)
    if req.stability_classes is None:
        classes = list(STABILITY_CLASSES)
    else:
        classes = list(dict.fromkeys(req.stability_classes))  # 去重保序
        if not classes:
            raise PlumeInputError("稳定度列表为空：至少需要一个稳定度等级")

    # ---- 静风硬拦截：先于一切受体/浓度计算，失败即整体取消 ----
    if not math.isfinite(met.wind_speed_ms) or met.wind_speed_ms < 0.0:
        raise PlumeInputError("风速缺失或非法")
    if met.wind_speed_ms < req.calm_threshold_ms:
        raise CalmWindError(
            f"风速 {met.wind_speed_ms:.3g} m/s 低于静风阈值 "
            f"{req.calm_threshold_ms:.3g} m/s：稳定度扫描整体取消，"
            "不计算任何受体（定常高斯烟羽输运假设失效）。"
        )

    h_eff, rise_detail = effective_height(src, met, req.plume_rise.use_plume_rise)

    # ---- 受体（真实坐标）-> 烟羽坐标；任一非法距离即整体失败 ----
    lons = np.array([p[0] for p in req.receptors], dtype=float)
    lats = np.array([p[1] for p in req.receptors], dtype=float)
    bad: list[str] = []
    for i, (lo, la) in enumerate(zip(lons, lats)):
        if not (math.isfinite(lo) and math.isfinite(la)):
            bad.append(f"#{i + 1} 坐标非有限值")
        elif not (-180.0 <= lo <= 180.0 and -85.0 <= la <= 85.0):
            bad.append(f"#{i + 1} 坐标 ({lo}, {la}) 超出经纬度范围")
    if bad:
        raise PlumeInputError(
            "受体坐标非法，扫描整体取消、不产生部分结果：" + "；".join(bad)
        )

    e_arr, n_arr = lonlat_to_local(lons, lats, src.lon, src.lat)
    x_arr, y_arr = local_to_plume_coords(e_arr, n_arr, met.wind_from_deg)
    bad = []
    for i, x in enumerate(x_arr):
        xi = float(x)
        if not math.isfinite(xi):
            bad.append(f"#{i + 1} 下风向距离非有限值")
        elif xi <= 0.0:
            bad.append(
                f"#{i + 1} 下风向距离 x={xi:.1f} m ≤ 0"
                "（受体不在下风向：位于上风向或源点）"
            )
    if bad:
        raise PlumeInputError(
            "存在非法受体距离，扫描整体取消、不产生部分结果：" + "；".join(bad)
        )

    briggs = req.parameterization == "briggs_rural"
    bg = float(met.background_conc_ug_m3)
    e2d = np.asarray(e_arr, dtype=float).reshape(1, -1)
    n2d = np.asarray(n_arr, dtype=float).reshape(1, -1)

    def evaluate(cls: str, east: np.ndarray, north: np.ndarray) -> dict:
        return compute_plume_field(
            emission_rate_g_s=src.emission_rate_g_s,
            wind_speed_ms=met.wind_speed_ms,
            wind_from_deg=met.wind_from_deg,
            stability_class=cls,
            effective_height_m=h_eff,
            east_m=east,
            north_m=north,
            parameterization=req.parameterization,
            power_law=req.power_law,
            calm_threshold_ms=req.calm_threshold_ms,
        )

    # 中心线 (y=0) 峰值：对数加密扫描 + 黄金分割求精，与单点求值同一模型入口
    theta = math.radians(transport_bearing_deg(met.wind_from_deg))
    sin_t, cos_t = math.sin(theta), math.cos(theta)

    def centerline(cls: str, x: float) -> float:
        res = evaluate(cls, np.array([[x * sin_t]]), np.array([[x * cos_t]]))
        return float(res["field"][0, 0])

    scan_x = np.geomspace(_SWEEP_SCAN_LO_M, _SWEEP_SCAN_HI_M, _SWEEP_SCAN_N)

    results = []
    for cls in classes:
        res = evaluate(cls, e2d, n2d)
        plume = res["field"][0]
        sy = res["sigma_y_m"][0]
        sz = res["sigma_z_m"][0]
        rows = []
        for i in range(len(req.receptors)):
            flags, in_range = _sweep_flags(float(x_arr[i]), briggs)
            rows.append(
                {
                    "receptor_index": i,
                    "downwind_x_m": float(x_arr[i]),
                    "plume_ug_m3": float(plume[i]),
                    "background_ug_m3": bg,
                    "total_ug_m3": float(plume[i] + bg),
                    "sigma_y_m": float(sy[i]),
                    "sigma_z_m": float(sz[i]),
                    "in_valid_range": in_range,
                    "flags": flags,
                }
            )

        scan_c = evaluate(
            cls,
            (scan_x * sin_t).reshape(1, -1),
            (scan_x * cos_t).reshape(1, -1),
        )["field"][0]
        imax = int(np.argmax(scan_c))
        if 0 < imax < len(scan_x) - 1:
            px, pv = _golden_max_scalar(
                lambda xv: centerline(cls, xv),
                float(scan_x[imax - 1]),
                float(scan_x[imax + 1]),
            )
            peak_flags, peak_in_range = _sweep_flags(px, briggs)
        else:  # 峰值落在扫描边界：不假装精确，显式标记
            px, pv = float(scan_x[imax]), float(scan_c[imax])
            peak_flags, peak_in_range = _sweep_flags(px, briggs)
            peak_flags = peak_flags + [FLAG_PEAK_EDGE]
        isample = int(np.argmax([r["total_ug_m3"] for r in rows]))
        results.append(
            {
                "stability": cls,
                "stability_cn": STABILITY_DESCRIPTIONS[cls]["name_cn"],
                "rows": rows,
                "peak": {
                    "downwind_x_m": px,
                    "plume_ug_m3": pv,
                    "background_ug_m3": bg,
                    "total_ug_m3": pv + bg,
                    "in_valid_range": peak_in_range,
                    "flags": peak_flags,
                    "method": (
                        f"中心线(y=0)对数加密扫描 {_SWEEP_SCAN_N} 点 "
                        f"[{_SWEEP_SCAN_LO_M:g}, {_SWEEP_SCAN_HI_M:g}] m "
                        "+ 黄金分割求精"
                    ),
                    "scan_interval_m": [_SWEEP_SCAN_LO_M, _SWEEP_SCAN_HI_M],
                },
                "sampled_peak": {
                    "receptor_index": isample,
                    "downwind_x_m": rows[isample]["downwind_x_m"],
                    "total_ug_m3": rows[isample]["total_ug_m3"],
                },
            }
        )

    receptors = []
    for i in range(len(req.receptors)):
        flags, in_range = _sweep_flags(float(x_arr[i]), briggs)
        receptors.append(
            {
                "index": i,
                "lonlat": [float(lons[i]), float(lats[i])],
                "east_north_m": [float(e_arr[i]), float(n_arr[i])],
                "downwind_x_m": float(x_arr[i]),
                "crosswind_y_m": float(y_arr[i]),
                "in_valid_range": in_range,
                "flags": flags,
            }
        )

    if briggs:
        coefficients: object = [
            item
            for item in parameterization_metadata()
            if item["stability"] in classes
        ]
        valid_range: list[float] | None = [
            settings.briggs_valid_x_min_m,
            settings.briggs_valid_x_max_m,
        ]
        range_note = (
            "Briggs 乡村系数建议适用 x ≈ 100 m–10 km；"
            "范围外的点已逐行标记，仅作趋势演示，不视为精确预测"
        )
    else:
        plp = PowerLawParams(**req.power_law) if req.power_law else DEFAULT_POWER_LAW
        coefficients = {
            "formula": "sigma_y = ay*x^py; sigma_z = az*x^pz",
            "ay": plp.ay,
            "py": plp.py,
            "az": plp.az,
            "pz": plp.pz,
        }
        valid_range = None
        range_note = (
            "幂律参数化用于解析核对，无物理适用范围标定；"
            "全部结果仅作教学对照，不视为精确预测"
        )

    return {
        "trial_note": (
            "独立试算：固定源项与风（风速/风向/背景），仅在本请求内临时替换"
            "稳定度；不读取、不写回已保存气象情景，结果不保存。"
            "静风或任一受体距离非法即整体失败，不产生部分结果。"
        ),
        "source": {
            "name": src.name,
            "lonlat": [src.lon, src.lat],
            "pollutant": src.pollutant,
            "stack_height_m": src.stack_height_m,
            "emission_rate_g_s": src.emission_rate_g_s,
        },
        "fixed_conditions": {
            "wind_from_deg": met.wind_from_deg,
            "transport_bearing_deg": transport_bearing_deg(met.wind_from_deg),
            "wind_speed_ms": met.wind_speed_ms,
            "background_conc_ug_m3": bg,
            "effective_stack_height_m": float(h_eff),
            "plume_rise_delta_h_m": float(rise_detail["delta_h_m"]),
            "calm_threshold_ms": req.calm_threshold_ms,
            "stability_note": "稳定度逐类临时替换；气象情景记录不被修改",
        },
        "units": {
            "distance": "m",
            "concentration": "μg/m³",
            "wind_speed": "m/s",
            "emission_rate": "g/s",
            "conversion": "g → μg 系数 1e6",
        },
        "parameterization": {
            "name": req.parameterization,
            "valid_range_m": valid_range,
            "range_note": range_note,
            "coefficients": coefficients,
        },
        "flag_legend": FLAG_LEGEND,
        "stability_classes": classes,
        "receptors": receptors,
        "results": results,
        "disclaimer": DISCLAIMER,
    }
