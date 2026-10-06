<script setup lang="ts">
import { computed, ref } from 'vue'
import { api } from '../api'
import type { FormState } from '../form'
import type {
  SourceRow,
  StabilityClass,
  StabilitySweepResponse,
  SweepPoint,
} from '../types'

/**
 * 稳定度扫描（独立试算）：
 * 固定源项/风速/风向，对 A–F 稳定度在一组下风向受体距离上批量求值。
 * 只发计算请求，不修改任何已保存气象情景。
 */
const props = defineProps<{
  form: FormState
  source: SourceRow | null
}>()

const ALL_CLASSES: StabilityClass[] = ['A', 'B', 'C', 'D', 'E', 'F']
const CLASS_CN: Record<StabilityClass, string> = {
  A: '极不稳定',
  B: '不稳定',
  C: '弱不稳定',
  D: '中性',
  E: '较稳定',
  F: '稳定',
}
/** 各稳定度曲线配色（分类色，与地图色带无关）。 */
const CLASS_COLOR: Record<StabilityClass, string> = {
  A: '#d62728',
  B: '#ff7f0e',
  C: '#8c7b00',
  D: '#1f77b4',
  E: '#2ca02c',
  F: '#9467bd',
}

const distancesText = ref('100, 200, 500, 1000, 2000, 3500, 5000, 7500, 10000')
const classes = ref<StabilityClass[]>([...ALL_CLASSES])
const result = ref<StabilitySweepResponse | null>(null)
const error = ref<string | null>(null)
const loading = ref(false)
const logScale = ref(true)
const quantity = ref<'total' | 'plume'>('total')

const isCalm = computed(() => props.form.windSpeed < props.form.calmThreshold)

function toggleClass(c: StabilityClass) {
  const i = classes.value.indexOf(c)
  if (i >= 0) classes.value.splice(i, 1)
  else classes.value.push(c)
  classes.value.sort((a, b) => ALL_CLASSES.indexOf(a) - ALL_CLASSES.indexOf(b))
}

/** 解析距离文本；非法时抛出带说明的 Error（不发请求）。 */
function parseDistances(): number[] {
  const tokens = distancesText.value.split(/[,，;；\s]+/).filter((t) => t.length > 0)
  if (!tokens.length) throw new Error('请至少输入一个下风向受体距离')
  const out = tokens.map((t) => Number(t))
  const bad = tokens.filter((t, i) => !Number.isFinite(out[i]))
  if (bad.length) throw new Error(`距离含非数值项：${bad.join(', ')}`)
  const nonPos = out.filter((d) => d <= 0)
  if (nonPos.length) throw new Error(`受体距离必须为正的下风向距离：${nonPos.join(', ')}`)
  const tooFar = out.filter((d) => d > 50000)
  if (tooFar.length) throw new Error(`受体距离超出模型上限 50000 m：${tooFar.join(', ')}`)
  if (out.length > 200) throw new Error('受体距离最多 200 个')
  return out
}

async function run() {
  error.value = null
  result.value = null
  let distances: number[]
  try {
    distances = parseDistances()
  } catch (e: any) {
    error.value = e.message
    return
  }
  if (!props.source) {
    error.value = '排放源尚未加载'
    return
  }
  if (!classes.value.length) {
    error.value = '请至少勾选一个稳定度等级'
    return
  }
  const f = props.form
  const s = props.source
  loading.value = true
  try {
    result.value = await api.stabilitySweep({
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
        name: '稳定度扫描试算',
        wind_from_deg: f.windFrom,
        wind_speed_ms: f.windSpeed,
        stability_class: f.stability, // 占位，将被扫描的稳定度覆盖
        ambient_temp_k: f.ambientT,
        pressure_hpa: f.pressure,
        background_conc_ug_m3: f.background,
      },
      receptor_distances_m: distances,
      stability_classes: [...classes.value],
      plume_rise: { use_plume_rise: f.useRise },
      parameterization: f.parameterization,
      power_law:
        f.parameterization === 'power_law'
          ? { ay: f.ay, py: f.py, az: f.az, pz: f.pz }
          : null,
      calm_threshold_ms: f.calmThreshold,
    })
  } catch (e: any) {
    result.value = null
    error.value = e.apiError?.message || e.message || '试算失败'
  } finally {
    loading.value = false
  }
}

/* ---------- 曲线图（SVG，无第三方库） ---------- */

const CHART = { w: 340, h: 208, ml: 46, mr: 8, mt: 10, mb: 30 }

function pointValue(p: { plume_conc_ug_m3: number; total_conc_ug_m3: number }) {
  return quantity.value === 'total' ? p.total_conc_ug_m3 : p.plume_conc_ug_m3
}

const sortedDistances = computed(() => {
  if (!result.value) return []
  return result.value.receptors.map((r) => r.distance_m).sort((a, b) => a - b)
})

/** 各稳定度按当前距离排序后的序列（值取当前展示量）。 */
const series = computed(() => {
  if (!result.value) return []
  return result.value.results.map((blk) => {
    const pts = [...blk.points].sort((a, b) => a.distance_m - b.distance_m)
    return { blk, pts }
  })
})

const yDomain = computed<[number, number]>(() => {
  const vals: number[] = []
  for (const s of series.value)
    for (const p of s.pts) {
      const v = pointValue(p)
      if (logScale.value ? v > 0 : Number.isFinite(v)) vals.push(v)
    }
  if (!vals.length) return [0, 1]
  let lo = Math.min(...vals)
  let hi = Math.max(...vals)
  if (logScale.value) {
    lo = Math.max(lo, hi * 1e-6)
    return [lo / 1.5, hi * 1.5]
  }
  if (lo === hi) hi = lo + 1
  return [0, hi * 1.08]
})

function xScale(d: number): number {
  const ds = sortedDistances.value
  const d0 = ds[0] ?? 1
  const d1 = ds[ds.length - 1] ?? 2
  const { w, ml, mr } = CHART
  if (logScale.value) {
    const l0 = Math.log10(d0)
    const l1 = Math.log10(d1 === d0 ? d0 * 10 : d1)
    return ml + ((Math.log10(d) - l0) / (l1 - l0)) * (w - ml - mr)
  }
  return ml + ((d - d0) / (d1 - d0 || 1)) * (w - ml - mr)
}

function yScale(v: number): number {
  const [lo, hi] = yDomain.value
  const { h, mt, mb } = CHART
  if (logScale.value) {
    const l0 = Math.log10(lo)
    const l1 = Math.log10(hi)
    return h - mb - ((Math.log10(Math.max(v, lo)) - l0) / (l1 - l0)) * (h - mt - mb)
  }
  return h - mb - ((v - lo) / (hi - lo)) * (h - mt - mb)
}

function linePath(pts: SweepPoint[]): string {
  const parts: string[] = []
  for (const p of pts) {
    const v = pointValue(p)
    if (logScale.value && v <= 0) continue
    parts.push(`${parts.length ? 'L' : 'M'}${xScale(p.distance_m).toFixed(1)},${yScale(v).toFixed(1)}`)
  }
  return parts.join(' ')
}

const xTicks = computed(() => {
  const ds = sortedDistances.value
  if (!ds.length) return []
  if (!logScale.value || ds[ds.length - 1] / ds[0] < 50) {
    const n = Math.min(5, ds.length)
    const step = (ds.length - 1) / Math.max(n - 1, 1)
    const out: number[] = []
    for (let i = 0; i < n; i++) out.push(ds[Math.round(i * step)])
    return [...new Set(out)]
  }
  // 对数轴取 1-2-5 刻度
  const lo = Math.floor(Math.log10(ds[0]))
  const hi = Math.ceil(Math.log10(ds[ds.length - 1]))
  const ticks: number[] = []
  for (let e = lo; e <= hi; e++)
    for (const m of [1, 2, 5]) {
      const v = m * 10 ** e
      if (v >= ds[0] && v <= ds[ds.length - 1]) ticks.push(v)
    }
  return ticks
})

const yTicks = computed(() => {
  const [lo, hi] = yDomain.value
  if (logScale.value) {
    const ticks: number[] = []
    for (let e = Math.ceil(Math.log10(lo)); e <= Math.floor(Math.log10(hi)); e++)
      ticks.push(10 ** e)
    return ticks.length ? ticks : [lo, hi]
  }
  const out: number[] = []
  for (let i = 0; i <= 4; i++) out.push(lo + ((hi - lo) * i) / 4)
  return out
})

function fmtTick(v: number): string {
  if (v === 0) return '0'
  if (v >= 1000 || v < 0.01) return v.toExponential(0)
  return String(Math.round(v * 100) / 100)
}

function fmt(v: number, d = 2): string {
  if (!Number.isFinite(v)) return '—'
  if (v !== 0 && (Math.abs(v) >= 10000 || Math.abs(v) < 0.01)) return v.toExponential(2)
  return v.toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d })
}

/* ---------- 表格 ---------- */

const tableRows = computed(() => {
  if (!result.value) return []
  const r = result.value
  return r.receptors
    .map((rec, i) => ({
      rec,
      cells: r.results.map((blk) => blk.points[i]),
    }))
    .sort((a, b) => a.rec.distance_m - b.rec.distance_m)
})

/* ---------- CSV 导出（带单位、系数、适用范围标记） ---------- */

function csvEscape(s: string): string {
  return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

function exportCsv() {
  const r = result.value
  if (!r || !props.source) return
  const f = props.form
  const L: string[] = []
  L.push('# 高斯烟羽稳定度扫描（独立试算，不写回任何已保存气象情景）')
  L.push(`# 生成时间: ${new Date().toISOString()}`)
  L.push(
    `# 源: ${csvEscape(props.source.name)} lon=${r.source_lonlat[0]} lat=${r.source_lonlat[1]}` +
      `; Q_g_s=${f.emission}; H_m=${f.stackHeight}; H_eff_m=${r.effective_stack_height_m}` +
      `; 抬升Δh_m=${r.plume_rise_delta_h_m}`,
  )
  L.push(
    `# 气象(固定): u_m_s=${r.wind.wind_speed_ms}; wind_from_deg=${r.wind.wind_from_deg}` +
      `; transport_bearing_deg=${r.wind.transport_bearing_deg}` +
      `; 背景_ug_m3=${r.background_conc_ug_m3}`,
  )
  L.push(`# 参数化: ${r.coefficients.parameterization}`)
  if (r.coefficients.per_stability) {
    for (const c of r.coefficients.per_stability)
      L.push(
        `# 系数 ${c.stability}(${c.stability_cn}): sigma_y=${c.sigma_y}` +
          `; sigma_z_form=${c.sigma_z_form}` +
          `; sigma_z_constants=${JSON.stringify(c.sigma_z_constants)}` +
          `; 适用x_m=${c.x_valid_range_m[0]}-${c.x_valid_range_m[1]}`,
      )
  } else if (r.coefficients.coeffs) {
    const c = r.coefficients.coeffs
    L.push(
      `# 系数: ${r.coefficients.formula}; ay=${c.ay}; py=${c.py}; az=${c.az}; pz=${c.pz}` +
        '（幂律无官方适用范围）',
    )
  }
  L.push(`# 适用范围: ${r.validity.out_of_range_policy}`)
  L.push(`# 峰值说明: ${r.validity.peak_note}`)
  L.push(`# 一致性: ${r.validity.evaluation_path}`)
  L.push(`# 单位: 距离/σ 为 m，浓度为 μg/m³（g→μg ×1e6），风速 m/s`)
  L.push(`# 免责声明: ${r.disclaimer}`)

  const header = ['distance_m', 'receptor_lon', 'receptor_lat', 'in_valid_range', 'range_flag']
  for (const c of r.stability_classes)
    header.push(`${c}_plume_ug_m3`, `${c}_background_ug_m3`, `${c}_total_ug_m3`)
  L.push(header.join(','))

  const rows = r.receptors
    .map((rec, i) => ({ rec, cells: r.results.map((blk) => blk.points[i]) }))
    .sort((a, b) => a.rec.distance_m - b.rec.distance_m)
  for (const { rec, cells } of rows) {
    const p0 = cells[0]
    const inRange =
      p0.within_valid_range === null ? 'n/a' : p0.within_valid_range ? 'true' : 'false'
    const cols = [
      String(rec.distance_m),
      String(rec.lonlat[0]),
      String(rec.lonlat[1]),
      inRange,
      p0.range_flag,
    ]
    for (const p of cells)
      cols.push(
        String(p.plume_conc_ug_m3),
        String(p.background_conc_ug_m3),
        String(p.total_conc_ug_m3),
      )
    L.push(cols.join(','))
  }

  const blob = new Blob(['\uFEFF' + L.join('\n')], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  const stamp = new Date().toISOString().replace(/[:T]/g, '').slice(0, 12)
  a.href = URL.createObjectURL(blob)
  a.download = `stability_sweep_${stamp}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}
</script>

<template>
  <div class="section">
    <h2>稳定度扫描（独立试算）</h2>
    <p class="muted" style="font-size: 11px; line-height: 1.5">
      固定当前源项、风速 u={{ form.windSpeed }} m/s、风向
      {{ form.windFrom }}° 与背景值，对一组下风向受体距离逐稳定度求值。
      仅试算，<b>不修改任何已保存气象情景</b>。
    </p>

    <label class="field">
      <span class="lbl">下风向受体距离（m，逗号/空格分隔，1–200 个，≤ 50000）</span>
      <textarea
        data-test="sweep-distances"
        class="num"
        rows="2"
        v-model="distancesText"
        style="width: 100%; font-family: ui-monospace, monospace; font-size: 11px"
      />
    </label>

    <div class="field">
      <span class="lbl" style="display: block; margin-bottom: 3px">扫描稳定度</span>
      <label
        v-for="c in ALL_CLASSES"
        :key="c"
        class="toggle"
        style="display: inline-flex; margin-right: 8px; margin-bottom: 2px"
      >
        <input
          type="checkbox"
          :checked="classes.includes(c)"
          @change="toggleClass(c)"
        />
        <span :style="{ color: CLASS_COLOR[c], fontWeight: 600 }">{{ c }}</span>
        <span class="muted" style="font-size: 10px">{{ CLASS_CN[c] }}</span>
      </label>
    </div>

    <div style="display: flex; gap: 8px; align-items: center; margin-bottom: 8px">
      <button data-test="sweep-run" @click="run" :disabled="loading || isCalm">
        {{ loading ? '试算中…' : '运行稳定度扫描' }}
      </button>
      <span v-if="isCalm" class="badge bad">静风，已停用</span>
    </div>
    <div v-if="isCalm" class="notice err">
      静风（u={{ form.windSpeed }} m/s &lt; 阈值 {{ form.calmThreshold }} m/s）：
      定常高斯烟羽不适用，扫描将被后端整体拒绝（422），不产生任何部分结果。
    </div>
    <div v-if="error" class="notice err">{{ error }}</div>

    <template v-if="result">
      <div class="notice info" style="font-size: 11px">
        {{ result.trial_note }} 同一受体、同一稳定度的数值与「单点求值」
        （POST /api/plume/points）逐列一致，可交叉核对。
      </div>

      <div style="display: flex; gap: 12px; align-items: center; margin: 6px 0 2px">
        <label class="toggle" style="margin: 0">
          <input type="checkbox" v-model="logScale" /> 对数坐标轴
        </label>
        <label class="toggle" style="margin: 0">
          <input type="radio" value="total" v-model="quantity" /> 总浓度
        </label>
        <label class="toggle" style="margin: 0">
          <input type="radio" value="plume" v-model="quantity" /> 烟羽贡献
        </label>
      </div>

      <svg
        data-test="sweep-chart"
        :viewBox="`0 0 ${CHART.w} ${CHART.h}`"
        style="width: 100%; background: #fbfcfe; border: 1px solid var(--line); border-radius: 4px"
      >
        <!-- 网格与坐标轴 -->
        <g v-for="t in yTicks" :key="'y' + t">
          <line
            :x1="CHART.ml" :x2="CHART.w - CHART.mr"
            :y1="yScale(t)" :y2="yScale(t)"
            stroke="#e3e8ef" stroke-width="1"
          />
          <text :x="CHART.ml - 4" :y="yScale(t) + 3" text-anchor="end" font-size="8" fill="#697586">
            {{ fmtTick(t) }}
          </text>
        </g>
        <g v-for="t in xTicks" :key="'x' + t">
          <line
            :x1="xScale(t)" :x2="xScale(t)"
            :y1="CHART.mt" :y2="CHART.h - CHART.mb"
            stroke="#eef1f5" stroke-width="1"
          />
          <text :x="xScale(t)" :y="CHART.h - CHART.mb + 11" text-anchor="middle" font-size="8" fill="#697586">
            {{ fmtTick(t) }}
          </text>
        </g>
        <text :x="CHART.w / 2" :y="CHART.h - 4" text-anchor="middle" font-size="9" fill="#3c4a5a">
          下风向距离 x（m）
        </text>
        <text
          :x="10" :y="CHART.h / 2" text-anchor="middle" font-size="9" fill="#3c4a5a"
          :transform="`rotate(-90 10 ${CHART.h / 2})`"
        >
          {{ quantity === 'total' ? '总浓度' : '烟羽贡献' }}（μg/m³）
        </text>

        <!-- 各稳定度曲线 -->
        <g v-for="s in series" :key="s.blk.stability_class">
          <path
            :d="linePath(s.pts)"
            fill="none"
            :stroke="CLASS_COLOR[s.blk.stability_class]"
            stroke-width="1.6"
          />
          <circle
            v-for="p in s.pts"
            :key="p.distance_m"
            :cx="xScale(p.distance_m)"
            :cy="yScale(pointValue(p))"
            r="1.6"
            :fill="CLASS_COLOR[s.blk.stability_class]"
          >
            <title>
              {{ s.blk.stability_class }} 类 x={{ p.distance_m }} m：
              {{ fmt(pointValue(p)) }} μg/m³
              {{ p.within_valid_range === false ? '（超出适用范围，仅趋势）' : '' }}
            </title>
          </circle>
          <!-- 峰值标记 -->
          <circle
            :cx="xScale(s.blk.peak_on_receptors.distance_m)"
            :cy="yScale(
              quantity === 'total'
                ? s.blk.peak_on_receptors.total_conc_ug_m3
                : s.blk.peak_on_receptors.plume_conc_ug_m3,
            )"
            r="4"
            fill="none"
            :stroke="CLASS_COLOR[s.blk.stability_class]"
            stroke-width="1.8"
            stroke-dasharray="2 1.6"
          >
            <title>
              {{ s.blk.stability_class }} 类采样峰值：x={{
                s.blk.peak_on_receptors.distance_m
              }}
              m（所给受体中的最大值）
            </title>
          </circle>
        </g>
      </svg>
      <div class="muted" style="font-size: 10px; margin: 2px 0 8px">
        虚线圈＝该稳定度在所给受体上的采样峰值（非连续峰值解析解）。
      </div>

      <!-- 峰值位置汇总 -->
      <h2>峰值位置（受体采样最大值）</h2>
      <div class="tbl-scroll">
      <table class="meta-tbl sweep-tbl" data-test="sweep-peaks">
        <tr>
          <th>稳定度</th><th>x_peak (m)</th>
          <th>{{ quantity === 'total' ? '总浓度' : '烟羽' }}峰值 (μg/m³)</th>
        </tr>
        <tr v-for="s in series" :key="'pk' + s.blk.stability_class">
          <td>
            <span :style="{ color: CLASS_COLOR[s.blk.stability_class], fontWeight: 700 }">
              {{ s.blk.stability_class }}
            </span>
            {{ s.blk.stability_cn }}
          </td>
          <td class="mono">{{ fmt(s.blk.peak_on_receptors.distance_m, 0) }}</td>
          <td class="mono">
            {{
              fmt(
                quantity === 'total'
                  ? s.blk.peak_on_receptors.total_conc_ug_m3
                  : s.blk.peak_on_receptors.plume_conc_ug_m3,
              )
            }}
          </td>
        </tr>
      </table>
      </div>

      <!-- 逐距离表格 -->
      <h2 style="margin-top: 10px">
        逐距离浓度（{{ quantity === 'total' ? '总浓度' : '烟羽贡献' }}，μg/m³）
      </h2>
      <div class="tbl-scroll">
      <table class="meta-tbl sweep-tbl" data-test="sweep-table">
        <tr>
          <th>x (m)</th>
          <th v-for="c in result.stability_classes" :key="'h' + c">
            <span :style="{ color: CLASS_COLOR[c] }">{{ c }}</span>
          </th>
          <th>适用范围</th>
        </tr>
        <tr v-for="row in tableRows" :key="'r' + row.rec.distance_m">
          <td class="mono">{{ fmt(row.rec.distance_m, 0) }}</td>
          <td
            v-for="(p, ci) in row.cells"
            :key="ci"
            class="mono"
            :class="{ oob: p.within_valid_range === false }"
            :title="
              p.within_valid_range === false
                ? '超出模型适用范围，仅作趋势演示，非精确预测'
                : p.range_flag
            "
          >
            {{ fmt(pointValue(p)) }}
          </td>
          <td class="mono" style="font-size: 10px">
            <span v-if="row.cells[0].within_valid_range === true">✓ 范围内</span>
            <span v-else-if="row.cells[0].within_valid_range === false" class="oob">
              ⚠ {{ row.cells[0].range_flag === 'below_briggs_valid_range' ? '低于' : '高于' }}适用区
            </span>
            <span v-else class="muted">n/a</span>
          </td>
        </tr>
      </table>
      </div>
      <div class="muted" style="font-size: 10px; margin-top: 2px">
        背景值 {{ fmt(result.background_conc_ug_m3) }} μg/m³ 为空间常数，已含在总浓度中；
        灰色斜体单元格超出适用范围，仅作趋势演示。
      </div>

      <button class="ghost" data-test="sweep-export" style="margin-top: 8px" @click="exportCsv">
        导出 CSV（含单位、系数与适用范围标记）
      </button>
      <div class="notice warn" style="margin-top: 8px">{{ result.disclaimer }}</div>
    </template>
  </div>
</template>

<style scoped>
.tbl-scroll {
  overflow-x: auto;
  max-width: 100%;
}
.sweep-tbl td,
.sweep-tbl th {
  padding: 2px 4px;
  font-size: 10.5px;
  white-space: nowrap;
}
.sweep-tbl td.oob {
  color: #9aa5b1;
  font-style: italic;
  background: #f6f7f9;
}
</style>
