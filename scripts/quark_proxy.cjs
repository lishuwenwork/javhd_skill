'use strict';

// Project-owned preload, not a modification of the pinned official CLI.
// The CLI supplies its own agents, so overriding only globalAgent is insufficient.
const http = require('node:http');
const https = require('node:https');
const tls = require('node:tls');
const net = require('node:net');
const { URL, urlToHttpOptions } = require('node:url');
const { syncBuiltinESMExports } = require('node:module');

const originalHttpRequest = http.request.bind(http);
const originalHttpsRequest = https.request.bind(https);
const proxyValue = process.env.JAVHD_QUARK_PROXY || '';
const connectTimeout = 20000;
let proxy;

function failure(code, reason, destination, status) {
  const safeReason = /^[A-Z0-9_]+$/.test(reason || '') ? reason : 'CONNECTION_FAILED';
  const error = new Error(`${code}: ${safeReason}${status ? ` (HTTP ${status})` : ''}`);
  error.code = code;
  // Never include proxy URLs, request headers or authentication in diagnostics.
  const diagnostic = /(^|\.)(effirst\.com|wpk\.quark\.cn)$/.test(destination || '');
  process.stderr.write('JAVHD_QUARK_NETWORK_ERROR ' + JSON.stringify({
    code, reason: safeReason, critical: !diagnostic, status: status || undefined,
  }) + '\n');
  return error;
}

if (proxyValue) {
  try {
    proxy = new URL(proxyValue);
    if (!['http:', 'https:'].includes(proxy.protocol) || !proxy.hostname ||
        proxy.hash || proxy.search || (proxy.pathname && proxy.pathname !== '/')) {
      throw new Error('Invalid proxy');
    }
    decodeURIComponent(proxy.username);
    decodeURIComponent(proxy.password);
  } catch (_) {
    failure('QUARK_PROXY_CONFIG', 'INVALID_PROXY_URL', '');
    process.exit(78);
  }
}

function proxyAuthorization() {
  if (!proxy.username && !proxy.password) return undefined;
  return 'Basic ' + Buffer.from(
    decodeURIComponent(proxy.username) + ':' + decodeURIComponent(proxy.password), 'utf8',
  ).toString('base64');
}

function proxyOptions() {
  return {
    hostname: proxy.hostname.replace(/^\[|\]$/g, ''),
    port: proxy.port || (proxy.protocol === 'https:' ? 443 : 80),
    agent: false,
    rejectUnauthorized: true,
  };
}

class TunnelAgent extends https.Agent {
  createConnection(options, callback) {
    const host = String(options.hostname || options.host).replace(/^\[|\]$/g, '');
    const authority = `${net.isIP(host) === 6 ? `[${host}]` : host}:${options.port || 443}`;
    const headers = { Host: authority };
    const authorization = proxyAuthorization();
    if (authorization) headers['Proxy-Authorization'] = authorization;
    const send = proxy.protocol === 'https:' ? originalHttpsRequest : originalHttpRequest;
    let settled = false;
    const request = send({ ...proxyOptions(), method: 'CONNECT', path: authority, headers });
    const finish = (error, socket) => {
      if (settled) return;
      settled = true;
      request.setTimeout(0);
      callback(error, socket);
    };
    request.setTimeout(connectTimeout, () => request.destroy(Object.assign(new Error(), { code: 'ETIMEDOUT' })));
    request.once('connect', (response, socket, head) => {
      if (response.statusCode !== 200) {
        socket.destroy();
        finish(failure(response.statusCode === 407 ? 'QUARK_PROXY_AUTH' : 'QUARK_PROXY_CONNECT',
          'CONNECT_REJECTED', host, response.statusCode));
        return;
      }
      request.setTimeout(0);
      if (head.length) socket.unshift(head);
      const secure = tls.connect({
        socket,
        servername: net.isIP(host) ? undefined : host,
        rejectUnauthorized: true,
        ca: options.ca,
        ALPNProtocols: ['http/1.1'],
      });
      secure.setTimeout(connectTimeout, () => secure.destroy(Object.assign(new Error(), { code: 'ETIMEDOUT' })));
      secure.once('secureConnect', () => {
        secure.setTimeout(0);
        finish(null, secure);
      });
      secure.once('error', error => {
        if (!settled) finish(failure('QUARK_PROXY_TLS', error.code, host));
      });
    });
    request.once('error', error => {
      if (!settled) finish(failure('QUARK_PROXY_CONNECT', error.code, host));
    });
    request.end();
  }
}

class ForwardAgent extends http.Agent {
  createConnection(options, callback) {
    const endpoint = proxyOptions();
    let settled = false;
    const socket = proxy.protocol === 'https:'
      ? tls.connect({ host: endpoint.hostname, port: endpoint.port, rejectUnauthorized: true,
        servername: net.isIP(endpoint.hostname) ? undefined : endpoint.hostname })
      : net.connect({ host: endpoint.hostname, port: endpoint.port });
    const finish = (error, connected) => {
      if (settled) return;
      settled = true;
      socket.setTimeout(0);
      callback(error, connected);
    };
    socket.setTimeout(connectTimeout, () => socket.destroy(Object.assign(new Error(), { code: 'ETIMEDOUT' })));
    socket.once(proxy.protocol === 'https:' ? 'secureConnect' : 'connect', () => finish(null, socket));
    socket.once('error', error => {
      if (!settled) finish(failure('QUARK_PROXY_CONNECT', error.code, options.hostname));
    });
  }
}

function normalize(args, protocol) {
  let first = args[0];
  let options;
  let callback;
  if (typeof first === 'string' || first instanceof URL) {
    options = urlToHttpOptions(new URL(first));
    if (args[1] && typeof args[1] === 'object') Object.assign(options, args[1]);
  } else {
    options = { ...first };
  }
  for (const arg of args) if (typeof arg === 'function') callback = arg;
  options.protocol = options.protocol || protocol;
  return { options, callback };
}

function copyHeaders(value) {
  if (!Array.isArray(value)) return { ...value };
  const headers = {};
  for (let index = 0; index < value.length; index += 2) headers[value[index]] = value[index + 1];
  return headers;
}

function removeHeader(headers, name) {
  for (const key of Object.keys(headers)) if (key.toLowerCase() === name) delete headers[key];
}

function observeErrors(request, host) {
  request.once('error', error => {
    // SDK token-refresh code can turn a transport error into a generic auth error.
    // Preserve a safe transport marker, including errors after TLS was established.
    if (!String(error.code || '').startsWith('QUARK_') && error.code !== 'ABORT_ERR') {
      failure(proxy ? 'QUARK_PROXY_REQUEST' : 'QUARK_DIRECT_REQUEST', error.code, host);
    }
  });
  return request;
}

if (proxy) {
  const tunnelAgent = new TunnelAgent({ keepAlive: true });
  const forwardAgent = new ForwardAgent({ keepAlive: true });
  https.request = function (...args) {
    const { options, callback } = normalize(args, 'https:');
    options.headers = copyHeaders(options.headers);
    removeHeader(options.headers, 'proxy-authorization');
    options.agent = tunnelAgent;
    return observeErrors(originalHttpsRequest(options, callback), options.hostname || options.host);
  };
  http.request = function (...args) {
    const { options, callback } = normalize(args, 'http:');
    const host = String(options.hostname || options.host || 'localhost').replace(/^\[|\]$/g, '');
    const authority = `${net.isIP(host) === 6 ? `[${host}]` : host}${options.port ? `:${options.port}` : ''}`;
    const target = `http://${authority}${options.path || '/'}`;
    options.headers = copyHeaders(options.headers);
    removeHeader(options.headers, 'proxy-authorization');
    removeHeader(options.headers, 'host');
    options.headers.Host = authority;
    const authorization = proxyAuthorization();
    if (authorization) options.headers['Proxy-Authorization'] = authorization;
    options.hostname = proxy.hostname.replace(/^\[|\]$/g, '');
    delete options.host;
    options.port = proxy.port || (proxy.protocol === 'https:' ? 443 : 80);
    options.path = target;
    options.agent = forwardAgent;
    const request = originalHttpRequest(options, response => {
      if (response.statusCode === 407) {
        response.resume();
        request.destroy(failure('QUARK_PROXY_AUTH', 'PROXY_AUTH_REQUIRED', host, 407));
      } else if (callback) callback(response);
    });
    return observeErrors(request, host);
  };
} else {
  https.request = function (...args) {
    const { options, callback } = normalize(args, 'https:');
    return observeErrors(originalHttpsRequest(options, callback), options.hostname || options.host);
  };
  http.request = function (...args) {
    const { options, callback } = normalize(args, 'http:');
    return observeErrors(originalHttpRequest(options, callback), options.hostname || options.host);
  };
}
// Node's native get() otherwise closes over its original request() function.
https.get = (...args) => { const request = https.request(...args); request.end(); return request; };
http.get = (...args) => { const request = http.request(...args); request.end(); return request; };
syncBuiltinESMExports();

// Read-only TLS reachability check, no official CLI, OAuth, config or file upload.
if (require.main === module) {
  const request = https.get('https://open-api-drive.quark.cn/', { timeout: connectTimeout }, response => {
    response.resume();
    response.once('end', () => {
      process.stdout.write(JSON.stringify({ code: 0, mode: proxy ? 'proxy' : 'direct',
        http_status: response.statusCode, tls_verified: true }) + '\n');
      process.exit(0);
    });
  });
  request.once('timeout', () => request.destroy(Object.assign(new Error(), { code: 'ETIMEDOUT' })));
  request.once('error', error => {
    process.stdout.write(JSON.stringify({ code: 1, mode: proxy ? 'proxy' : 'direct',
      reason: /^[A-Z0-9_]+$/.test(error.code || '') ? error.code : 'CONNECTION_FAILED' }) + '\n');
    process.exit(1);
  });
}
