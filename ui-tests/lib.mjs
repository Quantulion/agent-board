// Общая обвязка смоук-теста: временный AGENT_BOARD_HOME, CLI, сервер, API-клиент, ожидания.
import { spawn, execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { python } from './python.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
export const repo = path.resolve(here, '..');
export const out = path.join(here, 'out');

const envs = [];

/** Останавливает все серверы и удаляет временные каталоги всех созданных окружений. */
export async function cleanupAll() {
  await Promise.all(envs.splice(0).map(e => e.cleanup().catch(() => {})));
}

export const sleep = ms => new Promise(r => setTimeout(r, ms));
export const script = steps => 'FAKE-SCRIPT: ' + JSON.stringify(steps);

export function withTimeout(promise, ms, what) {
  return Promise.race([promise, new Promise((_, rej) => setTimeout(() => rej(new Error('таймаут: ' + what)), ms))]);
}

export async function until(fn, ms, what) {
  const deadline = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > deadline) throw new Error('не дождались: ' + what);
    await sleep(200);
  }
}

/** Изолированное окружение: свой каталог данных, проект `slug`, CLI и управление сервером.
 *  Язык проекта обязателен: ожидания текстов в сценариях не должны зависеть от языка по умолчанию. */
export function createEnv({ prefix, slug, title, language, config = {} }) {
  if (!language) throw new Error('createEnv: не задан язык проекта');
  const home = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  const env = { ...process.env, AGENT_BOARD_HOME: home, PYTHONPATH: path.join(repo, 'board'), AGENT_BOARD_PROJECT: slug, AGENT_BOARD_ACTOR: 'user', AGENT_BOARD_LANG: language };
  const cli = (...args) => execFileSync(python, ['-m', 'agent_board', ...args], { env, cwd: repo, encoding: 'utf-8', timeout: 30000 });
  fs.mkdirSync(out, { recursive: true });
  cli('init', slug, '--title', title);
  const cfgPath = path.join(home, 'projects', slug, 'project.json');
  const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'));
  fs.writeFileSync(cfgPath, JSON.stringify({ ...cfg, language, harness: { type: 'fake' }, ...config }, null, 2));

  const self = { home, cli, slug, proc: null, url: null, origin: null, token: null, port: null };

  self.start = async ({ runner = true } = {}) => {
    const port = self.port || 21000 + Math.floor(Math.random() * 2000);
    const args = ['-m', 'agent_board', 'serve', '--port', String(port)];
    if (!runner) args.push('--no-runner');
    const proc = spawn(python, args, { env, cwd: repo });
    proc.stderr.on('data', d => process.stderr.write(d));
    self.proc = proc;
    self.port = port;
    self.url = await withTimeout(new Promise((resolve, reject) => {
      let buf = '';
      proc.stdout.on('data', d => { buf += d; const m = buf.match(/http:\/\/\S+/); if (m) resolve(m[0]); });
      proc.on('exit', code => reject(new Error('сервер завершился: ' + code)));
    }), 15000, 'старт сервера');
    self.origin = new URL(self.url).origin;
    self.token = new URL(self.url).searchParams.get('token');
    return self;
  };

  self.stop = async () => {
    const proc = self.proc;
    if (!proc) return;
    self.proc = null;
    // уже завершившийся процесс второго события exit не пришлёт — ждать нечего
    if (proc.exitCode !== null || proc.signalCode !== null) return;
    const exited = new Promise(r => proc.once('exit', r));
    proc.kill('SIGTERM');
    await withTimeout(exited, 15000, 'остановка сервера').catch(() => proc.kill('SIGKILL'));
  };

  self.api = async (method, p, body) => {
    const resp = await fetch(`${self.origin}/api/p/${slug}/${p}`, {
      method, headers: { 'Content-Type': 'application/json', 'X-Board-Token': self.token, 'X-Board-Lang': language }, body: body ? JSON.stringify(body) : undefined,
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(p + ': ' + data.error);
    return data;
  };
  self.post = (p, body) => self.api('POST', p, body);
  self.state = () => self.api('GET', 'state');
  self.task = async key => (await self.state()).tasks.find(t => t.key === key);

  self.cleanup = async () => {
    await self.stop();
    fs.rmSync(home, { recursive: true, force: true });
  };
  envs.push(self);
  return self;
}
