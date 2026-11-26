import assert from 'node:assert/strict'
import { constants, generateKeyPairSync, privateDecrypt } from 'node:crypto'
import { once } from 'node:events'
import { createServer } from 'node:http'
import test from 'node:test'
import { createAuthMiddleware } from './auth.mjs'
import { authenticateXiaomai } from './xiaomai.mjs'

test('小麦登录加密密码并在签发登录凭证后通过验证', async () => {
  const { publicKey, privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 })
  const pem = publicKey.export({ type: 'spki', format: 'pem' })
  const paths = []
  const fetchImpl = async (url, init) => {
    paths.push(new URL(url).pathname)
    const body = JSON.parse(init.body)
    assert.equal(init.redirect, 'error')
    assert.deepEqual([body.account, body.loginClient, body.loginWay], ['测试账号', 0, 0])
    const encrypted = Buffer.from(body.pwd, 'base64')
    const encoded = privateDecrypt({ key: privateKey, padding: constants.RSA_NO_PADDING }, encrypted)
    assert.deepEqual([...encoded.subarray(0, 2)], [0, 2])
    assert.equal(encoded.subarray(encoded.indexOf(0, 2) + 1).toString(), '演示密码')
    return Response.json({ success: true, code: 200 }, { headers: { 'Access-Token': 'Bearer test-upstream-token' } })
  }

  await authenticateXiaomai('测试账号', '演示密码', { fetchImpl, publicKey: pem })
  assert.deepEqual(paths, ['/api/UserMicro/User/UserLogin'])

  await assert.rejects(authenticateXiaomai('测试账号', '演示密码', {
    fetchImpl: async () => Response.json({ success: true, code: 200 }),
  }), { status: 502, message: /登录凭证/ })
})

test('能耗接口需要有效会话，退出立即撤销访问', async (t) => {
  let authCalls = 0
  let energyReads = 0
  let regionDay
  const energy = { all: { value: 120.5 } }
  const auth = createAuthMiddleware({
    appOrigin: 'https://dashboard.test',
    authenticate: async () => { authCalls++ },
    loadEnergy: () => { energyReads++; return energy },
    loadRegionTemperature: (day) => {
      regionDay = day
      return { day, regions: [] }
    },
  })
  const server = createServer((req, res) => void auth(req, res, () => res.writeHead(404).end()))
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  t.after(() => server.close())
  const base = 'http://127.0.0.1:' + server.address().port
  const url = base + '/api/auth/'
  const getEnergy = (headers) => fetch(base + '/api/energy', { headers })
  const headers = { Origin: 'https://dashboard.test', 'Content-Type': 'application/json' }
  const login = (origin) => fetch(url + 'login', {
    method: 'POST',
    headers: { ...headers, Origin: origin },
    body: JSON.stringify({ account: 'test-user', password: 'test-password' }),
  })

  assert.equal((await getEnergy()).status, 401)
  const regionUrl = base + '/api/region-temperature'
  assert.equal((await fetch(regionUrl)).status, 401)
  assert.equal(energyReads, 0)
  assert.equal((await login('https://other.test')).status, 403)
  assert.equal(authCalls, 0)
  const loggedIn = await login(headers.Origin)
  assert.equal(loggedIn.status, 200)
  assert.equal(authCalls, 1)
  assert.equal(loggedIn.headers.get('Cache-Control'), 'no-store')
  const cookie = loggedIn.headers.get('Set-Cookie')
  assert.match(cookie, /HttpOnly; SameSite=Strict; Secure/)
  assert.deepEqual(await loggedIn.json(), { user: { account: 'test-user' } })

  const sessionHeaders = { Cookie: cookie.split(';')[0] }
  const session = () => fetch(url + 'session', { headers: sessionHeaders }).then((res) => res.json())
  assert.deepEqual(await session(), { user: { account: 'test-user' } })
  const data = await getEnergy(sessionHeaders)
  assert.equal(data.status, 200)
  assert.equal(data.headers.get('Cache-Control'), 'no-store')
  assert.deepEqual(await data.json(), energy)
  assert.equal(energyReads, 1)
  const regionData = await fetch(regionUrl + '?day=2026-10-17', { headers: sessionHeaders })
  assert.equal(regionData.status, 200)
  assert.equal(regionData.headers.get('Cache-Control'), 'no-store')
  assert.equal(regionDay, '2026-10-17')
  assert.equal((await fetch(regionUrl + '?day=wrong', { headers: sessionHeaders })).status, 400)
  const loggedOut = await fetch(url + 'logout', { method: 'POST', headers: { ...headers, ...sessionHeaders } })
  assert.equal(loggedOut.status, 200)
  assert.match(loggedOut.headers.get('Set-Cookie'), /Max-Age=0/)
  assert.deepEqual(await session(), { user: null })
  assert.equal((await getEnergy(sessionHeaders)).status, 401)
  assert.equal((await fetch(regionUrl, { headers: sessionHeaders })).status, 401)
  assert.equal(energyReads, 1)
})

test('小麦业务码 600 区分密码错误和其他上游失败', async () => {
  const loginWith = (message) => authenticateXiaomai('test-user', 'test-password', {
    fetchImpl: async () => Response.json({ success: false, code: 600, message }),
  })
  await assert.rejects(loginWith('账号或密码错误'), { status: 401 })
  await assert.rejects(loginWith('服务暂不可用'), { status: 502 })
})

test('小麦域名解析失败和连接超时分别提示', async () => {
  const loginWith = (error) => authenticateXiaomai('test-user', 'test-password', {
    fetchImpl: async () => { throw error },
  })
  await assert.rejects(loginWith(new TypeError('fetch failed', { cause: { code: 'ENOTFOUND' } })), {
    status: 502,
    message: /DNS/,
  })
  await assert.rejects(loginWith(new DOMException('timed out', 'TimeoutError')), { status: 504 })
})
