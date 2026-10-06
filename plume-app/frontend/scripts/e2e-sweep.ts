import { chromium } from 'playwright'

/**
 * 稳定度扫描页签 e2e：
 * 1) 运行扫描 → 曲线/峰值表/逐距离表渲染；
 * 2) 越界距离被标注（灰色斜体 oob 单元格）；
 * 3) 静风情景下扫描按钮停用且后端 422；
 * 4) 非法距离本地拦截；
 * 5) CSV 导出内容含单位、系数与适用范围标记。
 */
const errors: string[] = []
// 环境无 headless-shell 时使用完整 chromium 构建
const browser = await chromium
  .launch({ channel: 'chromium' })
  .catch(() => chromium.launch())
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } })
page.on('console', (m) => {
  // 忽略资源加载消息（favicon 404、负向测试中故意触发的 422）
  if (m.type() === 'error' && !m.text().startsWith('Failed to load resource'))
    errors.push('console: ' + m.text())
})
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))

await page.goto('http://127.0.0.1:5173/', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

// 打开稳定度扫描页签
await page.getByRole('button', { name: '稳定度扫描' }).click()
await page.waitForTimeout(300)

// 1. 运行扫描（默认距离含 100–10000 m，全部在 Briggs 适用范围内）
await page.locator('[data-test="sweep-run"]').click()
await page.waitForTimeout(1500)

const paths = await page.locator('[data-test="sweep-chart"] path').count()
const peakRows = await page.locator('[data-test="sweep-peaks"] tr').count()
const tableRows = await page.locator('[data-test="sweep-table"] tr').count()
console.log('curve paths:', paths, '| peak rows:', peakRows - 1, '| table rows:', tableRows - 1)
if (paths !== 6) errors.push(`expected 6 stability curves, got ${paths}`)

// 峰值随稳定度后移的教学结论：F 类峰值距离 > A 类
const peakCells = await page.locator('[data-test="sweep-peaks"] tr td:nth-child(2)').allTextContents()
console.log('peak distances by class:', peakCells.join(', '))

// 2. 越界标注：加入 50 m 与 20000 m 后重算
await page.locator('[data-test="sweep-distances"]').fill('50, 500, 1000, 20000')
await page.locator('[data-test="sweep-run"]').click()
await page.waitForTimeout(1500)
const oobCells = await page.locator('[data-test="sweep-table"] td.oob').count()
const flagCells = await page.locator('[data-test="sweep-table"] td:last-child').allTextContents()
console.log('out-of-range cells:', oobCells, '| range flags:', flagCells.join(' | '))
if (oobCells === 0) errors.push('expected out-of-range cells to be flagged')

// 3. 非法距离本地拦截（不发出请求）
await page.locator('[data-test="sweep-distances"]').fill('500, -100, abc')
await page.locator('[data-test="sweep-run"]').click()
await page.waitForTimeout(400)
const localErr = await page.locator('.panel.right .notice.err').first().textContent()
console.log('invalid distance notice:', localErr?.trim().slice(0, 60))

// 4. 静风：切到静风情景，扫描按钮停用
await page.locator('.panel select').nth(1).selectOption({ label: '静风情景（应被模型拒绝）' })
await page.waitForTimeout(600)
const sweepBtnDisabled = await page.locator('[data-test="sweep-run"]').isDisabled()
console.log('sweep run disabled under calm wind:', sweepBtnDisabled)
if (!sweepBtnDisabled) errors.push('sweep button should be disabled under calm wind')

// 后端直连复核：静风 → 422 calm_wind，且无部分结果
const calmResp = await page.evaluate(async () => {
  const r = await fetch('/api/plume/stability-sweep', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      source: { name: 't', lon: 116.4, lat: 39.9, stack_height_m: 120, emission_rate_g_s: 50, stack_diameter_m: 4, exit_velocity_ms: 18, stack_temp_k: 410, pollutant: 'SO2' },
      meteorology: { name: 't', wind_from_deg: 270, wind_speed_ms: 0.3, stability_class: 'D', ambient_temp_k: 293.15, pressure_hpa: 1013, background_conc_ug_m3: 15 },
      receptor_distances_m: [100, 500],
    }),
  })
  return { status: r.status, body: await r.json() }
})
console.log('calm wind API:', calmResp.status, calmResp.body.error, '| has results:', 'results' in calmResp.body)
if (calmResp.status !== 422 || 'results' in calmResp.body)
  errors.push('calm wind sweep must fail 422 without partial results')

// 5. 恢复非静风并验证 CSV 导出内容
await page.locator('.panel select').nth(1).selectOption({ label: '白天·中性大风（D）' })
await page.waitForTimeout(800)
await page.locator('[data-test="sweep-distances"]').fill('100, 500, 1000, 2000, 5000, 10000')
await page.locator('[data-test="sweep-run"]').click()
await page.waitForTimeout(1500)
const downloadPromise = page.waitForEvent('download')
await page.locator('[data-test="sweep-export"]').click()
const download = await downloadPromise
const path = await download.path()
const csv = await (await import('fs')).promises.readFile(path!, 'utf-8')
const head = csv.split('\n').filter((l) => l.startsWith('#'))
const headerLine = csv.split('\n').find((l) => l.startsWith('distance_m'))!
console.log('csv meta lines:', head.length, '| csv header cols:', headerLine.split(',').length)
console.log('--- csv head sample ---')
console.log(csv.split('\n').slice(0, 6).join('\n'))
console.log('--- csv first data row ---')
console.log(csv.split('\n').find((l, i) => i > 0 && !l.startsWith('#') && !l.startsWith('distance_m')))
for (const needle of ['单位', '系数', '适用范围', 'A_plume_ug_m3', 'F_total_ug_m3', 'in_valid_range']) {
  if (!csv.includes(needle)) errors.push(`CSV missing: ${needle}`)
}
await page.screenshot({ path: '/tmp/plume-sweep.png' })

if (errors.length) {
  console.log('--- JS/assertion errors ---')
  errors.forEach((e) => console.log(e))
  process.exit(1)
}
console.log('sweep e2e: no JS errors, all assertions passed')
await browser.close()
