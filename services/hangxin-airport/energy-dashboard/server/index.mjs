import { readFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { extname } from 'node:path'
import { createAuthMiddleware } from './auth.mjs'

const auth = createAuthMiddleware()
const port = Number(process.env.PORT ?? 4187)
const host = process.env.BIND_HOST ?? '127.0.0.1'
const dist = new URL('../dist/', import.meta.url)
const contentTypes = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.webp': 'image/webp',
}

async function servePage(request, response) {
  if (!['GET', 'HEAD'].includes(request.method)) {
    response.writeHead(405, { Allow: 'GET, HEAD' }).end()
    return
  }

  const path = request.url?.split('?')[0]
  const entry = path === '/' || /^\/(?:login|energy|region-temperature|building-3d|system-2d|ai-control)\/?$/.test(path ?? '')
  const asset = /^\/assets\/[\w.-]+\.(?:js|css|webp)$/.test(path ?? '')
  if (!entry && !asset) {
    response.writeHead(404).end()
    return
  }

  const file = entry ? 'index.html' : path.slice(1)
  try {
    const content = await readFile(new URL(file, dist))
    response.writeHead(200, {
      'Content-Type': contentTypes[extname(file)],
      'Content-Length': content.length,
      'Cache-Control': entry ? 'no-cache' : 'public, max-age=31536000, immutable',
      'X-Content-Type-Options': 'nosniff',
    })
    response.end(request.method === 'HEAD' ? undefined : content)
  } catch (error) {
    response.writeHead(error.code === 'ENOENT' ? 404 : 500).end()
  }
}

createServer({ requestTimeout: 20_000 }, (request, response) => {
  void auth(request, response, () => servePage(request, response))
}).listen(port, host, () => {
  console.log('看板已启动：http://' + host + ':' + port)
})
