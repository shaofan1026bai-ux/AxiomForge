import { randomBytes } from 'node:crypto'
import { parseEnergyQuery, readEnergy, readRegionTemperature } from './energy.mjs'
import { authenticateXiaomai, AuthError } from './xiaomai.mjs'

const cookieName = 'mangshi_session'
const sessionSeconds = 8 * 60 * 60
const attemptWindow = 60_000
const maxSessions = 2048
const maxClients = 1024

function reply(response, status, body) {
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff',
  })
  response.end(JSON.stringify(body))
}

function requestOrigin(request, configuredOrigin) {
  if (configuredOrigin) return configuredOrigin
  // 仅为本地预览推断来源。对外服务应配置浏览器实际访问的 HTTPS 地址。
  const host = request.headers.host ?? ''
  return /^(?:localhost|127\.0\.0\.1|\[::1\])(?::\d+)?$/i.test(host) ? 'http://' + host : null
}

function sessionCookie(id, maxAge, secure) {
  return `${cookieName}=${id}; Path=/; Max-Age=${maxAge}; HttpOnly; SameSite=Strict${secure ? '; Secure' : ''}`
}

function readSessionId(request) {
  const cookies = (request.headers.cookie ?? '').split(';')
  const id = cookies.find((cookie) => cookie.trimStart().startsWith(cookieName + '='))?.trim().slice(cookieName.length + 1)
  return /^[A-Za-z0-9_-]{43}$/.test(id ?? '') ? id : undefined
}

async function readCredentials(request) {
  if (request.headers['content-type']?.split(';')[0].trim() !== 'application/json') {
    throw new AuthError(415, '登录请求格式无效。')
  }
  const limit = 4096
  const chunks = []
  let size = 0
  // 超出上限时响应错误，但不累积剩余的请求体。
  for await (const chunk of request.iterator({ destroyOnReturn: false })) {
    size += chunk.length
    if (size > limit) throw new AuthError(413, '登录请求内容过长。')
    chunks.push(chunk)
  }
  let body
  try {
    body = JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    throw new AuthError(400, '登录请求格式无效。')
  }
  const account = typeof body?.account === 'string' ? body.account.trim() : ''
  const password = body?.password
  if (!account || account.length > 128 || typeof password !== 'string' || !password || password.length > 512) {
    throw new AuthError(400, '请输入有效的小麦账号和密码。')
  }
  return { account, password }
}

export function createAuthMiddleware({ authenticate = authenticateXiaomai, appOrigin = process.env.APP_ORIGIN, loadEnergy = (query) => readEnergy(undefined, query), loadRegionTemperature = (day, month) => readRegionTemperature(day, undefined, month) } = {}) {
  const configuredOrigin = appOrigin ? new URL(appOrigin).origin : null
  if (configuredOrigin && !configuredOrigin.startsWith('https://')) {
    throw new Error('APP_ORIGIN 必须使用 HTTPS。')
  }
  const sessions = new Map()
  const attempts = new Map()
  let inFlight = 0

  return async function auth(request, response, next) {
    const path = request.url?.split('?')[0] ?? ''
    const energyRoute = path === '/api/energy'
    const regionRoute = path === '/api/region-temperature'
    if (!energyRoute && !regionRoute && !path.startsWith('/api/auth/')) return next()
    const now = Date.now()
    const origin = requestOrigin(request, configuredOrigin)
    const id = readSessionId(request)
    const session = sessions.get(id)
    if (session && session.expiresAt <= now) sessions.delete(id)

    if (energyRoute || regionRoute) {
      if (request.method !== 'GET') {
        response.setHeader('Allow', 'GET')
        return reply(response, 405, { message: '请求方法不受支持。' })
      }
      if (!sessions.has(id)) return reply(response, 401, { message: '请登录后查看能耗数据。' })
      try {
        if (regionRoute) {
          const parameters = new URLSearchParams(request.url?.split('?')[1])
          const day = parameters.get('day') ?? undefined
          const month = parameters.get('month') ?? undefined
          if (month !== undefined && (!/^[1-9]\d{3}-(0[1-9]|1[0-2])$/.test(month) || day !== undefined)) {
            return reply(response, 400, { message: '请选择月份或日期。月份格式为 YYYY-MM。' })
          }
          if (day !== undefined && !/^\d{4}-\d{2}-\d{2}$/.test(day)) {
            return reply(response, 400, { message: '日期格式应为 YYYY-MM-DD。' })
          }
          return reply(response, 200, loadRegionTemperature(day, month))
        }
        const query = parseEnergyQuery(new URLSearchParams(request.url?.split('?')[1]))
        if (query.error) return reply(response, 400, { message: query.error })
        const energy = loadEnergy(query)
        if (!energy) return reply(response, 503, { message: '暂无已导入的历史能耗数据。' })
        return reply(response, 200, energy)
      } catch {
        return reply(response, 503, { message: regionRoute ? '暂时无法读取区域温度，请稍后重试。' : '暂时无法读取能耗数据，请稍后重试。' })
      }
    }

    try {
      if (path === '/api/auth/session' && request.method === 'GET') {
        return reply(response, 200, { user: sessions.get(id)?.user ?? null })
      }
      if (!['/api/auth/login', '/api/auth/logout', '/api/auth/session'].includes(path)) {
        return reply(response, 404, { message: '接口不存在。' })
      }
      if (request.method !== 'POST' || path === '/api/auth/session') {
        response.setHeader('Allow', path === '/api/auth/session' ? 'GET' : 'POST')
        return reply(response, 405, { message: '请求方法不受支持。' })
      }
      if (!origin || request.headers.origin !== origin) {
        return reply(response, 403, { message: '请求来源无效，请从看板页面操作。' })
      }

      const secure = origin.startsWith('https://')
      if (path === '/api/auth/logout') {
        sessions.delete(id)
        response.setHeader('Set-Cookie', sessionCookie('', 0, secure))
        return reply(response, 200, { user: null })
      }

      for (const [client, attempt] of attempts) {
        if (attempt.resetAt <= now) attempts.delete(client)
      }
      const client = request.socket.remoteAddress ?? 'unknown'
      const attempt = attempts.get(client)
      if (inFlight >= 4 || (attempt?.count ?? 0) >= 8 || (!attempt && attempts.size >= maxClients)) {
        response.setHeader('Retry-After', '60')
        return reply(response, 429, { message: '登录尝试过于频繁，请稍后重试。' })
      }
      attempts.set(client, { count: (attempt?.count ?? 0) + 1, resetAt: attempt?.resetAt ?? now + attemptWindow })

      inFlight++
      let account
      try {
        const credentials = await readCredentials(request)
        account = credentials.account
        await authenticate(account, credentials.password)
      } finally {
        inFlight--
      }

      const createdAt = Date.now()
      for (const [sessionId, entry] of sessions) {
        if (entry.expiresAt <= createdAt) sessions.delete(sessionId)
      }
      if (sessions.size >= maxSessions && !sessions.has(id)) {
        return reply(response, 503, { message: '登录服务暂时繁忙，请稍后重试。' })
      }
      sessions.delete(id)
      const sessionId = randomBytes(32).toString('base64url')
      const user = { account }
      sessions.set(sessionId, { user, expiresAt: createdAt + sessionSeconds * 1000 })
      response.setHeader('Set-Cookie', sessionCookie(sessionId, sessionSeconds, secure))
      return reply(response, 200, { user })
    } catch (error) {
      if (error instanceof AuthError) return reply(response, error.status, { message: error.message })
      return reply(response, 502, { message: '暂时无法连接小麦平台，请检查网络后重试。' })
    }
  }
}
