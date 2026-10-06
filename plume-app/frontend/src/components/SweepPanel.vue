<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api'
import type { FormState } from '../form'
import type {
  PlumePointRequest,
  SourceRow,
  StabilityClass,
  StabilitySweepRequest,
  StabilitySweepResponse,
} from '../types'

/**
 * 稳定度扫描（独立试算）：
 * 固定源项/风速/风向/背景（取自左侧面板当前值），仅替换稳定度 A–F，
 * 在一组按真实坐标定义的下风向受体上批量求值。
 * 不写回任何已保存气象情景；静风或非法距离 → 整体失败，不保留部分结果。
 */
const props = defineProps<{
  form: FormState
  source: SourceRow | null
}>()

const ALL_CLASSES: StabilityClass[] = ['A', 'B', 'C', 'D', 'E', 'F']
const CLASS_CN: Record<StabilityClass, string> = {
  A: '极不稳定', B: '不稳定', C: '弱不稳定', D: '中性', E: '较稳定', F: '稳定',
}
const CLASS_COLORS: Record<StabilityClass, string> = {
  A: '#d7191c', B: '#f07d02', C: '#c9a227', D: '#1a9850', E: '#2166ac', F: '#6a3d9a',
}

const distancesText = ref(
  '100, 200, 300, 500, 700, 1000, 1500, 2000, 3000, 4000, 5000, 7000, 10000, 15000, 20000',
)
const genFrom = ref(100)
const genTo = ref(20000)
const genN = ref(25)
const picked = ref<Record<StabilityClass, boolean>>({
  A: true, B: true, C: true, D: true, E: true, F: true,
})
const logX = ref(true)
const seriesMode = ref<'total' | 'plume'>('total')

const result = ref<StabilitySweepResponse | null>(null)
const error = ref('')
const loading = ref(false)
const lastRequest = ref<StabilitySweepRequest | null>(null)
const verifying = ref(false)
const verifyOk = ref<boolean | null>(null)
const verifyMsg = ref('')

const isCalm = computed(() => props.form.windSpeed < props.form.calmThreshold)

/** 当前面板输入的签名：与上次试算请求不一致时，旧结果标记为“已过期” */
function signatureOf(req: StabilitySweepRequest): string {
  return JSON.stringify([
    req.source.lon,
    req.source.lat,
    req.source.stack_height_m,
    req.source.emission_rate_g_s,
    req.source.stack_diameter_m,
    req.source.exit_velocity_ms,
    req.source.stack_temp_k,
    req.meteorology.wind_from_deg,
    req.meteorology.wind_speed_ms,
    req.meteorology.background_conc_ug_m3,
    req.meteorology.ambient_temp_k,
    req.meteorology.pressure_hpa,
    req.plume_rise.use_plume_rise,
    req.parameterization,
    req.power_law,
    req.calm_threshold_ms,
    req.receptors,
    req.stability_classes,
  ])
}

const stale = computed(() => {
  if (!result.value || !lastRequest.value || !props.source) return false
  if (parsed.value.errors.length || !parsed.value.distances.length) return true
  const current = buildRequest(receptorsFromDistances(parsed.value.distances))
  return signatureOf(current) !== signatureOf(lastRequest.value)
})

/** 解析距离文本：去重、升序；非法项逐条列出（不静默丢弃） */
const parsed = computed(() => {
  const tokens = distancesText.value.split(/[,，;；\s]+/).filter((t) => t.length > 0)
  const errs: string[] = []
  const vals: number[] = []
  tokens.forEach((t, i) => {
    const v = Number(t)
    if (!Number.isFinite(v)) errs.push(`第 ${i + 1} 项“${t}”不是数字`)
    else if (v <= 0) errs.push(`第 ${i + 1} 项 ${v} ≤ 0（下风向距离必须为正）`)
    else vals.push(v)
  })
  const distances = Array.from(new Set(vals)).sort((a, b) => a - b)
  return { distances, errors: errs, rawCount: tokens.length }
})

const pickedClasses = computed(() => ALL_CLASSES.filter((c) => picked.value[c]))

function genLogSpaced() {
  const lo = Math.min(genFrom.value, genTo.value)
  const hi = Math.max(genFrom.value, genTo.value)
  const n = Math.round(genN.value)
  if (!(lo > 0) || !(hi > lo) || !(n >= 2) || n > 200) {
    error.value = '生成参数非法：需要 0 < 起点 < 终点，2 ≤ 点数 ≤ 200'
    return
  }
  error.value = ''
  const vals: number[] = []
  for (let i = 0; i < n; i++) {
    const v = lo * Math.pow(hi / lo, i / (n - 1))
    vals.push(Math.round(v * 10) / 10)
  }
  distancesText.value = Array.from(new Set(vals)).join(', ')
}

const R_EARTH = 6371000
/** 下风向距离 -> 真实坐标受体（等距圆柱近似，与后端 geometry.py 同一约定） */
function receptorsFromDistances(distances: number[]): [number, number][] {
  const s = props.source!
  const theta = ((props.form.windFrom + 180) % 360) * (Math.PI / 180)
  const lat0 = (s.lat * Math.PI) / 180
  return distances.map((d) => {
    const e = d * Math.sin(theta)
    const n = d * Math.cos(theta)
    const lon = s.lon + (e / (R_EARTH * Math.cos(lat0))) * (180 / Math.PI)
    const lat = s.lat + (n / R_EARTH) * (180 / Math.PI)
    return [lon, lat] as [number, number]
  })
}

function buildRequest(receptors: [number, number][]): StabilitySweepRequest {
  const f = props.form
  const s = props.source!
  return {
    source: {
      name: s.name,
      lon: s.lon,
      lat: s.lat,
      stack_height_m: f.stackHeight,
      emission_rate_g_s: f.emission,
      stack_diameter_m: f.stackDia,
      exit_velocity_ms: f.exitV,
      stack_temp_k: f.stackT,
      pollutant: s.pollutant,
    },
    meteorology: {
      name: '界面情景（稳定度扫描试算）',
      wind_from_deg: f.windFrom,
      wind_speed_ms: f.windSpeed,
      stability_class: f.stability, // 占位：扫描时由后端逐类替换
      ambient_temp_k: f.ambientT,
      pressure_hpa: f.pressure,
      background_conc_ug_m3: f.background,
    },
    receptors,
    stability_classes: pickedClasses.value,
    plume_rise: { use_plume_rise: f.useRise },
    parameterization: f.parameterization,
    power_law:
      f.parameterization === 'power_law'
        ? { ay: f.ay, py: f.py, az: f.az, pz: f.pz }
        : null,
    calm_threshold_ms: f.calmThreshold,
  }
}

async function run() {
  // 试算失败或重算时不保留旧结果，避免把上一次表格误当本次输出
  error.value = ''
  verifyMsg.value = ''
  verifyOk.value = null
  result.value = null
  if (!props.source) {
    error.value = '未选择排放源'
    return
  }
  if (parsed.value.errors.length) {
    error.value = '受体距离非法：' + parsed.value.errors.join('；')
    return
  }
  if (!parsed.value.distances.length) {
    error.value = '请至少输入一个下风向受体距离'
    return
  }
  if (!pickedClasses.value.length) {
    error.value = '请至少勾选一个稳定度等级'
    return
  }
  if (isCalm.value) {
    error.value =
      `静风（u=${props.form.windSpeed} m/s < 阈值 ${props.form.calmThreshold} m/s）：` +
      '稳定度扫描整体取消，不计算任何受体。'
    return
  }
  const req = buildRequest(receptorsFromDistances(parsed.value.distances))
  loading.value = true
  try {
    result.value = await api.stabilitySweep(req)
    lastRequest.value = req
  } catch (e: any) {
    result.value = null // 失败的试算不留下任何结果
    error.value = e.apiError?.message || e.message || '扫描失败'
  } finally {
    loading.value = false
  }
}

/** 验收核对：用 /api/plume/points 对每个稳定度逐列复算本表 */
async function verify() {
  const res = result.value
  const base = lastRequest.value
  if (!res || !base) return
  verifying.value = true
  verifyMsg.value = ''
  verifyOk.value = null
  try {
    let maxDiff = 0
    for (const sr of res.results) {
      const preq: PlumePointRequest = {
        source: base.source,
        meteorology: { ...base.meteorology, stability_class: sr.stability },
        plume_rise: base.plume_rise,
        parameterization: base.parameterization,
        power_law: base.power_law ?? null,
        calm_threshold_ms: base.calm_threshold_ms,
        points: res.receptors.map((r) => [r.lonlat[0], r.lonlat[1]] as [number, number]),
      }
      const presp = await api.plumePoints(preq)
      sr.rows.forEach((row, i) => {
        const p = presp.points[i]
        maxDiff = Math.max(
          maxDiff,
          Math.abs(row.plume_ug_m3 - p.plume_conc_ug_m3),
          Math.abs(row.background_ug_m3 - p.background_conc_ug_m3),
          Math.abs(row.total_ug_m3 - p.total_conc_ug_m3),
        )
      })
    }
    verifyOk.value = maxDiff < 1e-6
    verifyMsg.value = verifyOk.value
      ? `核对通过：${res.results.length} 个稳定度 × ${res.receptors.length} 个受体，` +
        `烟羽/背景/总量逐列与单点求值一致（最大绝对偏差 ${maxDiff.toExponential(2)} μg/m³）。`
      : `核对不一致：最大绝对偏差 ${maxDiff.toExponential(2)} μg/m³`
  } catch (e: any) {
    verifyOk.value = false
    verifyMsg.value = '核对请求失败：' + (e.apiError?.message || e.message)
  } finally {
    verifying.value = false
  }
}

// ---------------------------------------------------------------------------
// 距离曲线图（纯 SVG，无第三方图表库）
// ---------------------------------------------------------------------------
const CH_W = 680
const CH_H = 340
const M = { l: 56, r: 14, t: 26, b: 40 }

function fmtTick(v: number): string {
  if (v >= 1000) return `${(v / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 1 })}k`
  if (v >= 100) return v.toFixed(0)
  if (v >= 1) return v.toFixed(1)
  return v.toPrecision(2)
}

const chart = computed(() => {
  const res = result.value
  if (!res) return null
  const mode = seriesMode.value
  const val = (o: { plume_ug_m3: number; total_ug_m3: number }) =>
    mode === 'total' ? o.total_ug_m3 : o.plume_ug_m3
  const xs = res.receptors.map((r) => r.downwind_x_m)
  const peakXs = res.results.map((r) => r.peak.downwind_x_m)
  const xMin = Math.min(...xs, ...peakXs)
  const xMax = Math.max(...xs, ...peakXs)
  const yMax =
    Math.max(
      ...res.results.flatMap((r) => r.rows.map((row) => val(row))),
      ...res.results.map((r) => val(r.peak)),
    ) * 1.08 || 1
  const sx = (x: number) => {
    const t = logX.value
      ? (Math.log10(x) - Math.log10(xMin)) / (Math.log10(xMax) - Math.log10(xMin) || 1)
      : (x - xMin) / (xMax - xMin || 1)
    return M.l + t * (CH_W - M.l - M.r)
  }
  const sy = (y: number) => M.t + (1 - y / yMax) * (CH_H - M.t - M.b)
  const series = res.results.map((sr, si) => {
    const pts = sr.rows
      .map((row) => ({ x: row.downwind_x_m, y: val(row), flags: row.flags }))
      .sort((a, b) => a.x - b.x)
    return {
      cls: sr.stability,
      cn: sr.stability_cn,
      idx: si,
      pts,
      line: pts.map((p) => `${sx(p.x)},${sy(p.y)}`).join(' '),
      peak: { x: sr.peak.downwind_x_m, y: val(sr.peak), flags: sr.peak.flags },
    }
  })
  let xticks: { x: number; label: string }[] = []
  if (logX.value) {
    xticks = [50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000]
      .filter((v) => v >= xMin && v <= xMax)
      .map((v) => ({ x: v, label: fmtTick(v) }))
  } else {
    for (let i = 0; i <= 4; i++) {
      const v = xMin + ((xMax - xMin) * i) / 4
      xticks.push({ x: v, label: fmtTick(v) })
    }
  }
  const yticks: { y: number; label: string }[] = []
  for (let i = 0; i <= 4; i++) {
    const v = (yMax * i) / 4
    yticks.push({ y: v, label: fmtTick(v) })
  }
  const vr = res.parameterization.valid_range_m
  const band =
    vr && Math.min(vr[1], xMax) > Math.max(vr[0], xMin)
      ? { x0: Math.max(vr[0], xMin), x1: Math.min(vr[1], xMax) }
      : null
  return { sx, sy, series, xticks, yticks, band, yMax }
})

function diamond(cx: number, cy: number, r: number): string {
  return `M ${cx} ${cy - r} L ${cx + r} ${cy} L ${cx} ${cy + r} L ${cx - r} ${cy} Z`
}

function peakLabel(x: number): string {
  return x < 1000 ? `${x.toFixed(0)} m` : `${(x / 1000).toFixed(2)} km`
}

function fmt(v: number, d = 2): string {
  if (!Number.isFinite(v)) return '—'
  return v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d })
}

function fmtCell(v: number): string {
  if (!Number.isFinite(v)) return '—'
  if (v >= 100) return v.toFixed(1)
  if (v >= 0.01) return v.toFixed(3)
  if (v === 0) return '0'
  return v.toExponential(2)
}

function flagTitle(flags: string[]): string {
  const legend = result.value?.flag_legend ?? {}
  return flags.map((f) => legend[f] ?? f).join('；')
}

// ---------------------------------------------------------------------------
// CSV 导出：带单位、系数与适用范围标记
// ---------------------------------------------------------------------------
function csvEscape(v: string): string {
  return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v
}

function coefficientsText(res: StabilitySweepResponse): string {
  const c = res.parameterization.coefficients
  if (res.parameterization.name === 'briggs_rural') {
    return (c as any[])
      .map(
        (it) =>
          `${it.stability}: σy=${it.sigma_y}; σz形式=${it.sigma_z_form} 常数=${JSON.stringify(it.sigma_z_constants)}`,
      )
      .join(' | ')
  }
  return `σy=${c.ay}*x^${c.py}; σz=${c.az}*x^${c.pz}（x、σ 单位 m）`
}

function exportCsv() {
  const res = result.value
  if (!res) return
  const fc = res.fixed_conditions
  const lines: string[] = []
  lines.push('# 高斯烟羽稳定度扫描（独立教学试算：不读取/不写回已保存气象情景，结果不保存）')
  lines.push(`# 生成时间: ${new Date().toISOString()}`)
  lines.push(
    `# 源: ${res.source.name}; 经纬度=(${res.source.lonlat[0]}, ${res.source.lonlat[1]}); ` +
      `烟囱高=${res.source.stack_height_m} m; 排放率=${res.source.emission_rate_g_s} g/s; 污染物=${res.source.pollutant}`,
  )
  lines.push(
    `# 固定风: 来向=${fc.wind_from_deg}° (输运方位=${fc.transport_bearing_deg}°); ` +
      `风速=${fc.wind_speed_ms} m/s; 背景=${fc.background_conc_ug_m3} μg/m³; ` +
      `有效源高=${fc.effective_stack_height_m} m (抬升Δh=${fc.plume_rise_delta_h_m} m)`,
  )
  lines.push('# 单位: 距离=m; 浓度=μg/m³; 风速=m/s; 排放率=g/s (g→μg ×1e6)')
  lines.push(`# 参数化: ${res.parameterization.name}; 适用范围: ${res.parameterization.range_note}`)
  lines.push(`# 系数: ${coefficientsText(res)}`)
  lines.push(
    `# 标记: ${Object.entries(res.flag_legend).map(([k, v]) => `${k}=${v}`).join(' | ')}`,
  )
  lines.push(
    `# 峰值(${res.results[0]?.peak.method ?? '中心线扫描'}): ` +
      res.results
        .map(
          (r) =>
            `${r.stability}: x=${r.peak.downwind_x_m.toFixed(1)} m, 总量=${r.peak.total_ug_m3.toPrecision(6)} μg/m³` +
            (r.peak.flags.length ? ` [${r.peak.flags.join(',')}]` : ''),
        )
        .join(' | '),
  )
  lines.push(`# 免责声明: ${res.disclaimer}`)

  const header = ['受体序号', '下风向距离x_m', '横风向y_m', '经度', '纬度', '适用范围内', '标记']
  for (const c of res.stability_classes) {
    header.push(`${c}_烟羽_μg/m³`, `${c}_背景_μg/m³`, `${c}_总量_μg/m³`)
  }
  lines.push(header.map(csvEscape).join(','))

  for (const rec of res.receptors) {
    const cells: (string | number)[] = [
      rec.index + 1,
      rec.downwind_x_m,
      rec.crosswind_y_m,
      rec.lonlat[0],
      rec.lonlat[1],
      rec.in_valid_range === null ? 'N/A' : rec.in_valid_range ? 'TRUE' : 'FALSE',
      rec.flags.join(';'),
    ]
    for (const sr of res.results) {
      const row = sr.rows.find((r) => r.receptor_index === rec.index)!
      cells.push(row.plume_ug_m3, row.background_ug_m3, row.total_ug_m3)
    }
    lines.push(cells.map((c) => csvEscape(String(c))).join(','))
  }

  const blob = new Blob(['\uFEFF' + lines.join('\n')], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `stability_sweep_${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="section">
    <h2>稳定度扫描（独立试算）</h2>
    <p class="muted" style="font-size:11px;line-height:1.6">
      固定源项与风（取自左侧面板当前值：u={{ form.windSpeed }} m/s，来向
      {{ form.windFrom }}°，背景 {{ form.background }} μg/m³），仅替换稳定度。
      不读取/不写回已保存气象情景；静风或非法距离会使整个试算失败，不保留部分结果。
    </p>

    <label class="field">
      <span class="lbl">下风向受体距离（m，逗号/空格分隔，沿烟羽中心线）</span>
      <textarea
        data-test="sweep-distances"
        class="num sweep-dist"
        rows="2"
        v-model="distancesText"
        spellcheck="false"
      />
    </label>
    <div class="row2" style="grid-template-columns: 1fr 1fr 1fr auto; align-items: end">
      <label class="field"><span class="lbl">起点 m</span>
        <input class="num" type="number" v-model.number="genFrom" /></label>
      <label class="field"><span class="lbl">终点 m</span>
        <input class="num" type="number" v-model.number="genTo" /></label>
      <label class="field"><span class="lbl">点数</span>
        <input class="num" type="number" v-model.number="genN" /></label>
      <button class="ghost" style="margin-bottom:8px" @click="genLogSpaced">对数等距生成</button>
    </div>
    <div class="muted" style="font-size:11px;margin-bottom:6px">
      已解析 {{ parsed.distances.length }} 个距离（去重升序）；
      受体经纬度 = 源点沿输运方位 {{ ((form.windFrom + 180) % 360).toFixed(0) }}° 推算。
      <span v-if="parsed.errors.length" style="color:var(--err)">
        {{ parsed.errors.join('；') }}
      </span>
    </div>

    <div class="sweep-classes">
      <label v-for="c in ALL_CLASSES" :key="c">
        <input type="checkbox" v-model="picked[c]" />
        <i class="dot" :style="{ background: CLASS_COLORS[c] }" />
        {{ c }} {{ CLASS_CN[c] }}
      </label>
    </div>

    <div class="btnrow">
      <button data-test="sweep-run" @click="run" :disabled="loading || isCalm">
        {{ loading ? '扫描中…' : '运行稳定度扫描' }}
      </button>
      <span v-if="isCalm" class="badge bad">静风，已停用</span>
    </div>
    <div v-if="isCalm" class="notice err">
      静风：u={{ form.windSpeed }} m/s &lt; 阈值 {{ form.calmThreshold }} m/s，
      定常高斯烟羽不适用，扫描被拒绝（不计算任何受体）。
    </div>
    <div v-if="error" class="notice err" data-test="sweep-error">{{ error }}</div>

    <template v-if="result">
      <div v-if="stale" class="notice warn" data-test="sweep-stale" style="font-size:11px">
        面板参数或受体距离自上次试算后已变化：下列结果对应<b>上次运行的条件</b>
        （各栏目数值以结果内记录为准），如需更新请重新运行扫描。
      </div>
      <div class="notice info" style="font-size:11px">
        {{ result.trial_note }}<br />
        有效源高 H_e = {{ fmt(result.fixed_conditions.effective_stack_height_m, 1) }} m
        （Δh = {{ fmt(result.fixed_conditions.plume_rise_delta_h_m, 1) }} m）；
        背景 {{ result.fixed_conditions.background_conc_ug_m3 }} μg/m³（空间常数，已含于总量列）。
        {{ result.parameterization.range_note }}
      </div>

      <div class="sweep-opts">
        <label class="toggle" style="margin:0">
          <input type="checkbox" v-model="logX" /> 距离对数轴
        </label>
        <label class="toggle" style="margin:0">
          <input type="radio" value="total" v-model="seriesMode" /> 总浓度
        </label>
        <label class="toggle" style="margin:0">
          <input type="radio" value="plume" v-model="seriesMode" /> 烟羽贡献
        </label>
      </div>

      <svg
        v-if="chart"
        data-test="sweep-chart"
        class="sweep-chart"
        :viewBox="`0 0 ${CH_W} ${CH_H}`"
        role="img"
      >
        <rect
          v-if="chart.band"
          :x="chart.sx(chart.band.x0)"
          :y="M.t"
          :width="chart.sx(chart.band.x1) - chart.sx(chart.band.x0)"
          :height="CH_H - M.t - M.b"
          fill="rgba(26,152,80,0.08)"
        />
        <g v-for="t in chart.xticks" :key="'x' + t.x">
          <line :x1="chart.sx(t.x)" :x2="chart.sx(t.x)" :y1="M.t" :y2="CH_H - M.b" stroke="#e3e8ee" />
          <text :x="chart.sx(t.x)" :y="CH_H - M.b + 14" text-anchor="middle" class="tick">{{ t.label }}</text>
        </g>
        <g v-for="t in chart.yticks" :key="'y' + t.y">
          <line :x1="M.l" :x2="CH_W - M.r" :y1="chart.sy(t.y)" :y2="chart.sy(t.y)" stroke="#e3e8ee" />
          <text :x="M.l - 6" :y="chart.sy(t.y) + 3" text-anchor="end" class="tick">{{ t.label }}</text>
        </g>
        <line :x1="M.l" :x2="CH_W - M.r" :y1="CH_H - M.b" :y2="CH_H - M.b" stroke="#94a3b8" />
        <line :x1="M.l" :x2="M.l" :y1="M.t" :y2="CH_H - M.b" stroke="#94a3b8" />
        <text :x="CH_W - M.r" :y="CH_H - 6" text-anchor="end" class="axis-lbl">
          下风向距离 x（m{{ logX ? '，对数轴' : '' }}）
        </text>
        <text :x="M.l" :y="14" class="axis-lbl">
          {{ seriesMode === 'total' ? '总浓度' : '烟羽贡献' }}（μg/m³）
        </text>
        <text v-if="chart.band" :x="chart.sx(chart.band.x0) + 4" :y="M.t + 10" class="band-lbl">
          Briggs 建议适用范围 0.1–10 km
        </text>
        <g v-for="s in chart.series" :key="s.cls">
          <polyline
            :points="s.line"
            fill="none"
            :stroke="CLASS_COLORS[s.cls]"
            stroke-width="1.8"
          />
          <circle
            v-for="(p, i) in s.pts"
            :key="i"
            :cx="chart.sx(p.x)"
            :cy="chart.sy(p.y)"
            r="2.6"
            :fill="p.flags.length ? '#fff' : CLASS_COLORS[s.cls]"
            :stroke="CLASS_COLORS[s.cls]"
            stroke-width="1.4"
          >
            <title>
              {{ s.cls }} 类 x={{ fmt(p.x, 0) }} m：{{ fmtCell(p.y) }} μg/m³
              {{ p.flags.length ? '（' + p.flags.join(', ') + '）' : '' }}
            </title>
          </circle>
          <path
            :d="diamond(chart.sx(s.peak.x), chart.sy(s.peak.y), 5.5)"
            :fill="CLASS_COLORS[s.cls]"
            stroke="#fff"
            stroke-width="1"
          >
            <title>
              {{ s.cls }} 类峰值：x≈{{ peakLabel(s.peak.x) }}，{{ fmtCell(s.peak.y) }} μg/m³
              {{ s.peak.flags.length ? '（' + s.peak.flags.join(', ') + '）' : '' }}
            </title>
          </path>
          <text
            :x="chart.sx(s.peak.x)"
            :y="chart.sy(s.peak.y) - 9 - (s.idx % 3) * 10"
            text-anchor="middle"
            class="peak-lbl"
            :fill="CLASS_COLORS[s.cls]"
          >
            {{ s.cls }}: {{ peakLabel(s.peak.x) }}
          </text>
        </g>
      </svg>
      <div class="muted" style="font-size:10px;margin:2px 0 8px">
        ◆ 为各稳定度中心线峰值（{{ result.results[0]?.peak.method }}）；
        空心点为适用范围外受体（仅趋势演示）；绿色带为 Briggs 建议适用范围。
      </div>

      <div class="peak-chips">
        <span
          v-for="sr in result.results"
          :key="sr.stability"
          class="chip"
          :style="{ borderColor: CLASS_COLORS[sr.stability] }"
        >
          <i class="dot" :style="{ background: CLASS_COLORS[sr.stability] }" />
          {{ sr.stability }}: 峰值 {{ fmtCell(sr.peak.total_ug_m3) }} @ {{ peakLabel(sr.peak.downwind_x_m) }}
          <em v-if="sr.peak.flags.length" :title="flagTitle(sr.peak.flags)">⚠</em>
        </span>
      </div>

      <div class="btnrow">
        <button class="ghost" data-test="sweep-verify" @click="verify" :disabled="verifying">
          {{ verifying ? '核对中…' : '用单点求值逐列核对' }}
        </button>
        <button class="ghost" data-test="sweep-export" @click="exportCsv">导出 CSV（含单位/系数/标记）</button>
      </div>
      <div
        v-if="verifyMsg"
        :class="['notice', verifyOk ? 'info' : 'err']"
        data-test="sweep-verify-msg"
        style="font-size:11px"
      >
        {{ verifyMsg }}
      </div>

      <div class="sweep-tbl-wrap">
        <table class="meta-tbl sweep-tbl" data-test="sweep-table">
          <thead>
            <tr>
              <th rowspan="2">#</th>
              <th rowspan="2">x (m)</th>
              <th rowspan="2">y (m)</th>
              <th rowspan="2">受体经纬度</th>
              <th rowspan="2">标记</th>
              <th
                v-for="sr in result.results"
                :key="sr.stability"
                colspan="2"
                :style="{ color: CLASS_COLORS[sr.stability] }"
              >
                {{ sr.stability }} {{ sr.stability_cn }}
              </th>
            </tr>
            <tr>
              <template v-for="sr in result.results" :key="sr.stability + '-sub'">
                <th>烟羽</th>
                <th>总量</th>
              </template>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="rec in result.receptors"
              :key="rec.index"
              :class="{ oor: rec.flags.length > 0 }"
            >
              <td>{{ rec.index + 1 }}</td>
              <td class="mono">{{ fmt(rec.downwind_x_m, 1) }}</td>
              <td class="mono">{{ fmt(rec.crosswind_y_m, 1) }}</td>
              <td class="mono">{{ rec.lonlat[0].toFixed(5) }}, {{ rec.lonlat[1].toFixed(5) }}</td>
              <td>
                <span v-if="!rec.flags.length" class="badge ok">范围内</span>
                <span
                  v-for="f in rec.flags"
                  :key="f"
                  class="badge bad"
                  :title="result.flag_legend[f]"
                >{{ f }}</span>
              </td>
              <template v-for="sr in result.results" :key="sr.stability">
                <td class="mono">{{ fmtCell(sr.rows[rec.index].plume_ug_m3) }}</td>
                <td class="mono"><b>{{ fmtCell(sr.rows[rec.index].total_ug_m3) }}</b></td>
              </template>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="muted" style="font-size:10px;margin-top:4px">
        单位：浓度 μg/m³，距离 m。背景 {{ result.fixed_conditions.background_conc_ug_m3 }} μg/m³
        为空间常数（总量 = 烟羽 + 背景）。黄色行为适用范围外：数值仅作趋势演示，非精确预测。
        表中任一格可用「用单点求值逐列核对」按钮或 POST /api/plume/points 复算。
      </div>
      <div class="notice warn" style="margin-top:8px">{{ result.disclaimer }}</div>
    </template>
    <div v-else-if="!error" class="muted" style="font-size:12px">
      设定受体距离后点击「运行稳定度扫描」。
    </div>
  </div>
</template>
