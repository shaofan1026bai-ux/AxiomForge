import { DatabaseSync } from 'node:sqlite'
import { existsSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const databasePath = fileURLToPath(new URL('../.local/energy.sqlite', import.meta.url))
const groupColors = {
  chiller: '#4055ee',
  midea: '#665fed',
  ahu: '#397da9',
  mitsubishi: '#8a93b3',
}

function dates(start, end) {
  const days = []
  const day = new Date(start)
  while (day.toISOString().slice(0, 10) <= end) {
    days.push(day.toISOString().slice(0, 10))
    day.setUTCDate(day.getUTCDate() + 1)
  }
  return days
}

export function parseEnergyQuery(params) {
  const start = params.get('start')
  const end = params.get('end')
  const comparisonMode = params.get('comparison') ?? 'mom'
  if (!['mom', 'yoy'].includes(comparisonMode)) return { error: '请选择有效的对比方式。' }
  if (start === null && end === null) return { comparisonMode }

  const validDay = (value) => {
    if (!/^[1-9]\d{3}-\d{2}-\d{2}$/.test(value ?? '')) return false
    const stamp = new Date(value)
    return Number.isFinite(stamp.getTime()) && stamp.toISOString().slice(0, 10) === value
  }
  if (!validDay(start) || !validDay(end) || start > end) return { error: '请选择有效的起止日期。' }
  if ((Date.parse(end) - Date.parse(start)) / 86400000 >= 366) return { error: '每次最多查询 366 天。' }
  return { start, end, comparisonMode }
}

function comparisonDay(day, mode) {
  const date = new Date(day)
  // 按每一天的日号对齐，遇到短月或非闰年时留空，避免重复计算某天。
  if (mode === 'yoy') date.setUTCFullYear(date.getUTCFullYear() - 1)
  else date.setUTCMonth(date.getUTCMonth() - 1)
  return date.getUTCDate() === Number(day.slice(8)) ? date.toISOString().slice(0, 10) : null
}

function total(values) {
  // 缺测读数不参与合计，全部缺测时保留空值。
  const available = values.filter((value) => value != null)
  if (!available.length) return null
  return Math.round(available.reduce((sum, value) => sum + value, 0) * 10000) / 10000
}

function dateRange(days) {
  if (!days.length) return '无对应日期'
  const start = days[0].replaceAll('-', '.')
  const end = days.at(-1).replaceAll('-', '.')
  if (start === end) return start
  return start + ' – ' + (start.slice(0, 4) === end.slice(0, 4) ? end.slice(5) : end)
}

export function readEnergy(path = databasePath, { start, end, comparisonMode = 'mom' } = {}) {
  if (!existsSync(path)) return null
  const db = new DatabaseSync(path, { readOnly: true })
  try {
    db.exec('BEGIN')
    const period = db.prepare('SELECT * FROM dashboard_period WHERE id = 1').get()
    const available = db.prepare('SELECT MIN(day) AS firstDay, MAX(day) AS lastDay FROM daily_energy WHERE kwh IS NOT NULL').get()
    if (!available.lastDay) return null
    const hasDailySync = db.prepare("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='daily_sync_runs'").get()
    const syncedDay = hasDailySync ? db.prepare('SELECT MAX(day) AS day FROM daily_sync_runs').get().day : null
    if (syncedDay > available.lastDay) available.lastDay = syncedDay
    let defaultStart = period?.start_day ?? available.lastDay
    let defaultEnd = period?.end_day ?? available.lastDay
    // 自动同步后向后滚动默认区间，沿用原区间天数及用户手选日期。
    if (syncedDay > defaultEnd) {
      const shift = Date.parse(syncedDay) - Date.parse(defaultEnd)
      defaultStart = new Date(Date.parse(defaultStart) + shift).toISOString().slice(0, 10)
      defaultEnd = syncedDay
    }
    const startDay = start ?? defaultStart
    const endDay = end ?? defaultEnd
    const currentDays = dates(startDay, endDay)
    const previousDays = currentDays.map((day) => comparisonDay(day, comparisonMode))
    const comparisonDates = previousDays.filter(Boolean)
    const dateParams = [startDay, endDay, comparisonDates[0] ?? null, comparisonDates.at(-1) ?? null]
    const meters = db.prepare('SELECT * FROM meters ORDER BY id').all()
    if (!meters.length || !currentDays.length) return null

    const records = db.prepare(`
      SELECT day, meter_id, kwh, estimated FROM daily_energy
      WHERE day BETWEEN ? AND ? OR day BETWEEN ? AND ?
    `).all(...dateParams)
    const includedDays = new Set([...currentDays, ...comparisonDates])
    const energyByKey = new Map(records.map((row) => [row.day + ':' + row.meter_id, row.kwh]))
    const weatherByDay = new Map(db.prepare(`
      SELECT day, temperature FROM daily_weather WHERE day BETWEEN ? AND ? OR day BETWEEN ? AND ?
    `).all(...dateParams)
      .map((row) => [row.day, row.temperature]))

    function summarize(id, name, scope) {
      const readings = (day) => scope.map((meter) => energyByKey.get(day + ':' + meter.id))
      const rows = currentDays.map((day, index) => {
        const previousDay = previousDays[index]
        const currentValues = readings(day)
        const comparisonValues = readings(previousDay)
        return {
          day,
          date: day.slice(5),
          comparisonDate: previousDay?.slice(5) ?? null,
          value: total(currentValues),
          comparison: total(comparisonValues),
          currentIncomplete: currentValues.some((value) => value == null),
          comparisonIncomplete: comparisonValues.some((value) => value == null),
          temperature: weatherByDay.get(day) ?? null,
          comparisonTemperature: weatherByDay.get(previousDay) ?? null,
        }
      })
      const value = total(rows.map((row) => row.value))
      const comparison = total(rows.map((row) => row.comparison))
      const currentMissingDays = rows.filter((row) => row.currentIncomplete).length
      const comparisonMissingDays = rows.filter((row) => row.comparisonIncomplete).length
      const changePercent = !currentMissingDays && !comparisonMissingDays && value !== null && comparison > 0
        ? (value - comparison) / comparison : null
      return { id, name, rows, value, comparison, changePercent, currentMissingDays, comparisonMissingDays }
    }

    const all = summarize('total', '已接入电表合计', meters)
    const groups = Map.groupBy(meters, (meter) => meter.group_key)
    const breakdown = [...groups].map(([id, scope]) => {
      const group = summarize(id, scope[0].group_name, scope)
      return {
        ...group,
        share: all.value > 0 && group.value !== null ? group.value / all.value : null,
        color: groupColors[id],
        devices: scope.map((meter) => summarize('meter-' + meter.id, meter.name, [meter])),
      }
    }).sort((a, b) => (b.share ?? 0) - (a.share ?? 0))

    return {
      all,
      breakdown,
      range: dateRange(currentDays),
      comparisonRange: dateRange(comparisonDates),
      currentMissingDays: all.currentMissingDays,
      comparisonMissingDays: all.comparisonMissingDays,
      query: { start: startDay, end: endDay, comparisonMode },
      available,
      hasEstimated: records.some((row) => row.estimated && includedDays.has(row.day)),
    }
  } finally {
    db.close()
  }
}

function monthlyRegionTemperature(db, days, month) {
  const start = month + '-01'
  const end = new Date(start)
  end.setUTCMonth(end.getUTCMonth() + 1, 0)
  const trendDays = dates(start, end.toISOString().slice(0, 10))
  const snapshots = db.prepare('SELECT day, regions_json FROM region_temperature_days WHERE day BETWEEN ? AND ? ORDER BY day')
    .all(start, trendDays.at(-1))
    .map((row) => [row.day, JSON.parse(row.regions_json)])
  const regionsByDay = new Map(snapshots.map(([day, items]) => [day, new Map(items.map((item) => [item.id, item]))]))
  const regions = new Map(snapshots.flatMap(([, items]) => items.map((item) => [item.id, item])))

  function summarize(findAverage) {
    const trend = trendDays.map((day) => findAverage(regionsByDay.get(day)) ?? null)
    const valid = trend.filter((value) => value !== null)
    return {
      trend,
      count: valid.length,
      average: valid.length ? valid.reduce((sum, value) => sum + value, 0) / valid.length : null,
    }
  }
  const hasWeather = db.prepare("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='daily_weather'").get()
  const weatherRows = hasWeather ? db.prepare('SELECT day, temperature FROM daily_weather WHERE day BETWEEN ? AND ?')
    .all(start, trendDays.at(-1)) : []
  const outdoorByDay = new Map(weatherRows.map((row) => [row.day, row.temperature]))
  return {
    days, day: null, month, trendDays,
    outdoor: trendDays.map((day) => outdoorByDay.get(day) ?? null),
    regions: [...regions.values()].map(({ id, name, floor, probes }) => ({
      id, name, floor,
      ...summarize((items) => items?.get(id)?.average),
      probes: probes.map(({ id: probeId, name, pending }) => ({
        id: probeId, name, pending,
        ...(pending ? {} : summarize((items) => {
          const reading = items?.get(id)?.probes.find((probe) => probe.id === probeId)
          return reading?.pending ? null : reading?.average
        })),
      })),
    })),
  }
}

export function readRegionTemperature(day, path = databasePath, month) {
  const empty = { days: [], day: null, regions: [] }
  if (!existsSync(path)) return empty
  const db = new DatabaseSync(path, { readOnly: true })
  try {
    db.exec('BEGIN')
    // 兼容只导入过能耗的数据库。
    if (!db.prepare("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='region_temperature_days'").get()) {
      return empty
    }
    const days = db.prepare('SELECT day FROM region_temperature_days ORDER BY day DESC').all().map((row) => row.day)
    if (month) return monthlyRegionTemperature(db, days, month)
    const selected = day ?? days[0]
    const row = selected && db.prepare('SELECT regions_json FROM region_temperature_days WHERE day = ?').get(selected)
    const hasOutdoor = db.prepare("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='outdoor_temperature_days'").get()
    const outdoorRow = row && hasOutdoor && db.prepare('SELECT trend_json FROM outdoor_temperature_days WHERE day = ?').get(selected)
    return {
      days, day: row ? selected : null,
      regions: row ? JSON.parse(row.regions_json) : [],
      outdoor: outdoorRow ? JSON.parse(outdoorRow.trend_json) : [],
    }
  } finally {
    db.close()
  }
}
