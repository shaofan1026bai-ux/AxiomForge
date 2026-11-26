import { constants, createPublicKey, publicEncrypt } from 'node:crypto'

const origin = 'https://smart-energy.yntravelsky.net.cn:36443'
const loginPath = '/api/UserMicro/User/UserLogin'

// 小麦接口公布的 RSA 公钥。部署可通过服务端环境变量覆盖，以支持公钥轮换。
const defaultPublicKey = `-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQC9yFhlGWb/RIbIL03vmRrC57kJ
g08nLLoxqeVU+/nQHxhmlvdfZlL7OJpg+BaKDJftwcdEBnQaawJRRQfIGPRlPJGn
muQQ/wGsfYaWFJaRrL9rYYAgPkTAePlbJJxRpkUxy2jEWt7zQrcMuIQbRRC7o8KV
tnBuCCvW+MEfcz23HQIDAQAB
-----END PUBLIC KEY-----`

export class AuthError extends Error {
  constructor(status, message) {
    super(message)
    this.status = status
  }
}

async function loginXiaomai(fetchImpl, body) {
  let response
  try {
    response = await fetchImpl(origin + loginPath, {
      method: 'POST',
      redirect: 'error',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(12000),
    })
  } catch (error) {
    const code = error.cause?.code
    if (code === 'ENOTFOUND' || code === 'EAI_AGAIN') {
      throw new AuthError(502, '无法解析小麦平台域名，请检查 VPN 或 DNS 后重试。')
    }
    if (error.name === 'TimeoutError' || code === 'UND_ERR_CONNECT_TIMEOUT') {
      throw new AuthError(504, '连接小麦平台超时，请稍后重试。')
    }
    throw error
  }

  if ([401, 403].includes(response.status)) {
    await response.body?.cancel()
    throw new AuthError(401, '小麦账号验证未通过，请检查账号和密码。')
  }
  if (!response.ok) {
    await response.body?.cancel()
    throw new AuthError(502, '暂时无法验证小麦账号，请稍后重试。')
  }

  let payload
  try {
    payload = await response.json()
  } catch (error) {
    if (error instanceof SyntaxError) {
      throw new AuthError(502, '小麦平台返回了无法识别的响应，请稍后重试。')
    }
    throw error
  }
  if (payload?.success !== true || payload?.code !== 200) {
    // 小麦用业务码 600 返回多种错误，仅将明确的账号密码错误映射为 401。
    const invalidCredentials = Number(payload?.code) === 600 && payload?.message === '账号或密码错误'
    if (payload?.success === false && (invalidCredentials || !(Number(payload.code) >= 500))) {
      throw new AuthError(401, '小麦账号验证未通过，请检查账号和密码。')
    }
    throw new AuthError(502, '小麦平台暂时无法完成账号验证。')
  }
  // 确认小麦已签发登录凭证。浏览器只收到看板自身的会话 Cookie。
  const token = response.headers.get('Access-Token')?.trim().replace(/^Bearer(?:\s+|$)/i, '').trim()
  if (!token) {
    throw new AuthError(502, '小麦平台未返回登录凭证，请稍后重试。')
  }
}

export async function authenticateXiaomai(account, password, {
  fetchImpl = fetch,
  publicKey = process.env.XIAOMAI_PUBLIC_KEY?.replaceAll('\\n', '\n') || defaultPublicKey,
} = {}) {
  let key
  try {
    key = createPublicKey(publicKey)
    if (key.asymmetricKeyType !== 'rsa') throw new Error()
  } catch {
    throw new AuthError(503, '小麦登录配置不可用，请联系管理员。')
  }

  const maxPasswordBytes = Math.ceil(key.asymmetricKeyDetails.modulusLength / 8) - 11
  if (Buffer.byteLength(password, 'utf8') > maxPasswordBytes) {
    throw new AuthError(400, '密码长度超出小麦登录接口支持的范围。')
  }
  const pwd = publicEncrypt({ key, padding: constants.RSA_PKCS1_PADDING }, Buffer.from(password)).toString('base64')

  await loginXiaomai(fetchImpl, { account, pwd, loginClient: 0, loginWay: 0 })
}
