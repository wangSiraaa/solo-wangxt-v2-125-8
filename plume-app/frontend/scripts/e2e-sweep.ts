import { chromium } from 'playwright'

// 稳定度扫描页签 e2e：曲线/表格渲染、单点逐列核对、CSV 导出、静风拦截
const errors: string[] = []
const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 1600, height: 900 } })
page.on('console', (m) => {
  if (m.type() === 'error') errors.push('console: ' + m.text())
})
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))

await page.goto('http://127.0.0.1:5173/', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)

// 1. 打开稳定度扫描页签并运行
await page.getByRole('button', { name: '稳定度扫描' }).click()
await page.getByRole('button', { name: '运行稳定度扫描' }).click()
await page.waitForSelector('[data-test="sweep-chart"]', { timeout: 15000 })

const curves = await page.locator('[data-test="sweep-chart"] polyline').count()
const peaks = await page.locator('[data-test="sweep-chart"] path').count()
const rows = await page.locator('[data-test="sweep-table"] tbody tr').count()
console.log('curves:', curves, '| peak markers:', peaks, '| table rows:', rows)
if (curves !== 6 || peaks !== 6 || rows !== 15) {
  console.log('UNEXPECTED sweep render counts')
  process.exit(1)
}

// 2. 适用范围外行被标注（默认距离含 100 m 以下无、15 km/20 km 超出 10 km）
const oorRows = await page.locator('[data-test="sweep-table"] tbody tr.oor').count()
console.log('out-of-range rows flagged:', oorRows)
if (oorRows !== 2) {
  console.log('EXPECTED 2 flagged rows (15000, 20000)')
  process.exit(1)
}

// 3. 单点求值逐列核对
await page.getByRole('button', { name: '用单点求值逐列核对' }).click()
await page.waitForSelector('[data-test="sweep-verify-msg"]', { timeout: 15000 })
const verifyText = await page.locator('[data-test="sweep-verify-msg"]').textContent()
console.log('verify:', verifyText?.trim())
if (!verifyText?.includes('核对通过')) {
  console.log('POINTS CROSS-CHECK FAILED')
  process.exit(1)
}

// 4. CSV 导出（含单位/系数/标记）
const downloadPromise = page.waitForEvent('download', { timeout: 10000 })
await page.getByRole('button', { name: /导出 CSV/ }).click()
const download = await downloadPromise
const path = await download.path()
const { readFileSync } = await import('fs')
const csv = readFileSync(path!, 'utf-8')
const mustHave = ['μg/m³', '系数', 'OUT_OF_BRIGGS_RANGE', 'A_烟羽_μg/m³', 'F_总量_μg/m³', '独立教学试算']
const missing = mustHave.filter((s) => !csv.includes(s))
console.log('csv lines:', csv.split('\n').length, '| missing tokens:', missing)
if (missing.length) {
  console.log('CSV CONTENT INCOMPLETE')
  process.exit(1)
}

// 5. 静风：切到静风情景后扫描按钮停用
await page.locator('.panel select').nth(1).selectOption({ label: '静风情景（应被模型拒绝）' })
await page.waitForTimeout(600)
const sweepBtnDisabled = await page
  .getByRole('button', { name: '运行稳定度扫描' })
  .isDisabled()
console.log('calm: sweep button disabled =', sweepBtnDisabled)
if (!sweepBtnDisabled) {
  console.log('SWEEP SHOULD BE DISABLED UNDER CALM WIND')
  process.exit(1)
}

await page.screenshot({ path: '/tmp/plume-sweep.png' })

if (errors.length) {
  console.log('--- JS errors ---')
  errors.forEach((e) => console.log(e))
  process.exit(1)
}
console.log('sweep e2e passed, no JS errors')
await browser.close()
