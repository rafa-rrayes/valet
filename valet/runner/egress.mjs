// Valet's egress proxy: one per container agent whose network isn't open (valet/runner/launch.py, agent.py).
//
// The agent's container sits on an internal Docker network of its own, which has no route out. This proxy is the
// only other container on it (as `valet-egress`), and is also on Docker's default bridge, so it is the agent's one
// way out. The agent's HTTP_PROXY and HTTPS_PROXY point here, and it lets through exactly what rules.json allows:
//
//   {"port": 3128,
//    "allow": [{"host": "api.anthropic.com", "ports": [443]},     // that host only
//              {"domain": "github.com", "ports": [80, 443]}]}     // that domain and its subdomains
//
// HTTPS, and any other protocol, goes through CONNECT tunnels; plain HTTP comes as absolute-form requests, each
// forwarded on a connection of its own (Connection: close). Anything not allowed gets a 403 that says why. Each
// decision is one JSON line on stdout, after a first {"event":"ready"} once it listens. It exits when its stdin
// closes: the runner, or the docker CLI between them, went away.
import fs from 'node:fs'
import net from 'node:net'

const HEAD_LIMIT = 64 * 1024        // bytes of request line and headers
const HEAD_TIMEOUT = 30_000         // ms to send them
const HOP_BY_HOP = new Set(['connection', 'keep-alive', 'proxy-connection', 'proxy-authorization', 'te', 'trailer',
  'upgrade'])

function log(event) {
  process.stdout.write(JSON.stringify(event) + '\n')
}

// Host names compare lowercase, without a trailing dot or IPv6 brackets.
function normalize(host) {
  host = String(host).toLowerCase()
  if (host.startsWith('[') && host.endsWith(']')) host = host.slice(1, -1)
  return host.endsWith('.') ? host.slice(0, -1) : host
}

function allowed(rules, host, port) {
  host = normalize(host)
  return rules.some((r) => (r.ports ?? []).includes(port)
    && (r.host !== undefined ? host === normalize(r.host)
      : host === normalize(r.domain) || host.endsWith('.' + normalize(r.domain))))
}

// "host:port" or "[v6]:port" -> [host, port], or null.
function authority(text) {
  const m = /^(\[[0-9a-fA-F:.]+\]|[^\s:/[\]@]+):(\d{1,5})$/.exec(text)
  if (!m) return null
  const port = Number(m[2])
  return port > 0 && port < 65536 ? [normalize(m[1]), port] : null
}

function refuse(client, status, reason, text) {
  const body = text + '\n'
  client.end(`HTTP/1.1 ${status} ${reason}\r\nContent-Type: text/plain; charset=utf-8\r\n`
    + `Content-Length: ${Buffer.byteLength(body)}\r\nConnection: close\r\n\r\n${body}`)
}

function denied(client, method, host, port) {
  log({ event: 'deny', method, host, port })
  refuse(client, 403, 'Forbidden', `Valet: ${host}:${port} is not on this agent's network allow list.`)
}

// Open the upstream connection; once it's up, `onUp(upstream)`, then pipe both ways until either side is done.
function relay(client, method, host, port, onUp) {
  const upstream = net.connect({ host, port, allowHalfOpen: true })
  let up = false
  upstream.on('connect', () => {
    up = true
    log({ event: 'allow', method, host, port })
    onUp(upstream)
    client.pipe(upstream)
    upstream.pipe(client)
  })
  upstream.on('error', (e) => {
    if (up) return client.destroy()
    log({ event: 'error', method, host, port, error: e.code ?? e.message })
    refuse(client, 502, 'Bad Gateway', `Valet: can't reach ${host}:${port} (${e.code ?? e.message}).`)
  })
  client.on('error', () => upstream.destroy())
  client.on('close', () => upstream.destroy())
}

function handle(rules, client, head, rest) {
  const lines = head.split('\r\n')
  const [method, target, version] = lines[0].split(' ')
  if (!method || !target || !/^HTTP\/1\.[01]$/.test(version ?? '')) {
    return refuse(client, 400, 'Bad Request', 'Valet: not an HTTP proxy request.')
  }
  if (method === 'CONNECT') {
    const hp = authority(target)
    if (!hp) return refuse(client, 400, 'Bad Request', 'Valet: CONNECT needs host:port.')
    const [host, port] = hp
    if (!allowed(rules, host, port)) return denied(client, method, host, port)
    return relay(client, method, host, port, (upstream) => {
      client.write('HTTP/1.1 200 Connection established\r\n\r\n')
      if (rest.length) upstream.write(rest)
    })
  }
  let url
  try {
    url = new URL(target)
  } catch {
    return refuse(client, 400, 'Bad Request', 'Valet: a proxy request needs an absolute http:// URL.')
  }
  if (url.protocol !== 'http:' || url.username || url.password) {
    return refuse(client, 400, 'Bad Request', 'Valet: a proxy request needs an absolute http:// URL.')
  }
  const host = normalize(url.hostname)
  const port = url.port ? Number(url.port) : 80
  if (!allowed(rules, host, port)) return denied(client, method, host, port)
  const headers = lines.slice(1).filter((l) => l && !HOP_BY_HOP.has(l.slice(0, l.indexOf(':')).trim().toLowerCase()))
  if (!headers.some((l) => /^host\s*:/i.test(l))) headers.unshift(`Host: ${url.host}`)
  const out = [`${method} ${url.pathname}${url.search} ${version}`, ...headers, 'Connection: close', '', ''].join('\r\n')
  relay(client, method, host, port, (upstream) => {
    upstream.write(out)
    if (rest.length) upstream.write(rest)
  })
}

function serve(rules, client) {
  let buf = Buffer.alloc(0)
  client.setTimeout(HEAD_TIMEOUT, () => client.destroy())
  client.on('error', () => {})
  const onData = (chunk) => {
    buf = Buffer.concat([buf, chunk])
    const end = buf.indexOf('\r\n\r\n')
    if (end < 0) {
      if (buf.length > HEAD_LIMIT) refuse(client, 431, 'Request Header Fields Too Large', 'Valet: headers too large.')
      return
    }
    client.removeListener('data', onData)
    client.pause()
    client.setTimeout(0)
    handle(rules, client, buf.subarray(0, end).toString('latin1'), buf.subarray(end + 4))
  }
  client.on('data', onData)
}

function main() {
  const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))
  const rules = config.allow ?? []
  const server = net.createServer({ allowHalfOpen: true }, (client) => serve(rules, client))
  server.listen(config.port ?? 3128, config.bind ?? '0.0.0.0', () => {
    log({ event: 'ready', port: server.address().port })
  })
  process.stdin.on('end', () => process.exit(0))
  process.stdin.on('error', () => process.exit(0))
  process.stdin.resume()
}

main()
