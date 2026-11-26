import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { DatabaseSync } from 'node:sqlite'
import test from 'node:test'
import { readEnergy } from './energy.mjs'

test('缺测时汇总已有读数，保留缺测日期和完整设备的对比', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'mangshi-energy-'))
  t.after(() => rmSync(directory, { recursive: true }))
  const path = join(directory, 'energy.sqlite')
  const db = new DatabaseSync(path)
  try {
    db.exec(`
      CREATE TABLE dashboard_period (id INTEGER, start_day TEXT, end_day TEXT);
      CREATE TABLE meters (id INTEGER, name TEXT, group_key TEXT, group_name TEXT);
      CREATE TABLE daily_energy (day TEXT, meter_id INTEGER, kwh REAL, estimated INTEGER);
      CREATE TABLE daily_weather (day TEXT, temperature REAL);
      INSERT INTO meters VALUES (1, 'AHU1电表', 'ahu', 'AHU总表'),
        (2, 'AHU2电表', 'ahu', 'AHU总表'), (3, '美的电表', 'midea', '美的空调电表');
      INSERT INTO daily_energy VALUES
        ('2026-11-01', 1, 10, 0), ('2026-11-01', 2, NULL, 0), ('2026-11-01', 3, 20, 0),
        ('2026-11-02', 1, 0, 0), ('2026-11-02', 2, 6, 0), ('2026-11-02', 3, 30, 0),
        ('2026-10-01', 1, 10, 0), ('2026-10-01', 2, 8, 0), ('2026-10-01', 3, 10, 0),
        ('2026-10-02', 1, 10, 0), ('2026-10-02', 2, 8, 0), ('2026-10-02', 3, 10, 0);
    `)
  } finally {
    db.close()
  }

  const { all, breakdown, currentMissingDays } = readEnergy(path, { start: '2026-11-01', end: '2026-11-02' })
  const ahu = breakdown.find(({ id }) => id === 'ahu')
  const midea = breakdown.find(({ id }) => id === 'midea')
  assert.equal(all.value, 66)
  assert.equal(all.comparison, 56)
  assert.equal(all.changePercent, null)
  assert.equal(currentMissingDays, 1)
  assert.deepEqual(all.rows.map(({ value, currentIncomplete }) => [value, currentIncomplete]), [[30, true], [36, false]])
  assert.equal(ahu.value, 16)
  assert.equal(ahu.share, 16 / 66)
  assert.equal(ahu.devices[0].rows[1].value, 0)
  assert.equal(ahu.devices[1].rows[0].value, null)
  assert.equal(ahu.devices[1].value, 6)
  assert.equal(midea.value, 50)
  assert.equal(midea.share, 50 / 66)
  assert.equal(midea.changePercent, 1.5)

  const empty = readEnergy(path, { start: '2026-11-03', end: '2026-11-03' })
  assert.equal(empty.all.value, null)
  assert.equal(empty.currentMissingDays, 1)
  assert.ok(empty.breakdown.every(({ share }) => share === null))

  const synced = new DatabaseSync(path)
  try {
    synced.exec(`
      INSERT INTO dashboard_period VALUES (1, '2026-11-01', '2026-11-02');
      CREATE TABLE daily_sync_runs (day TEXT PRIMARY KEY, synced_at TEXT);
      INSERT INTO daily_sync_runs VALUES ('2026-11-03', '2026-11-04T03:00:00+08:00');
    `)
  } finally {
    synced.close()
  }
  const updated = readEnergy(path)
  assert.equal(updated.available.lastDay, '2026-11-03')
  assert.deepEqual(updated.query, { start: '2026-11-02', end: '2026-11-03', comparisonMode: 'mom' })
  assert.equal(updated.all.rows[1].value, null)
  assert.equal(readEnergy(path, { start: '2026-11-01', end: '2026-11-01' }).all.value, 30)
})
