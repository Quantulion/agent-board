// Выбор интерпретатора Python для UI-тестов: работает и из основного checkout, и из worktree задачи.
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const venvPython = root => path.join(root, '.venv', 'bin', 'python');

function isExecutable(file) {
  try {
    fs.accessSync(file, fs.constants.X_OK);
    return fs.statSync(file).isFile();
  } catch {
    return false;
  }
}

/** Корень основного checkout: у worktree общий каталог .git лежит именно там. */
function mainCheckout() {
  try {
    const common = execFileSync('git', ['rev-parse', '--path-format=absolute', '--git-common-dir'],
      { cwd: repo, encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] }).trim();
    return common ? path.dirname(common) : null;
  } catch {
    return null;
  }
}

function fromPath(name) {
  for (const dir of (process.env.PATH || '').split(path.delimiter)) {
    if (dir && isExecutable(path.join(dir, name))) return path.join(dir, name);
  }
  return null;
}

/**
 * Порядок: AGENT_BOARD_PYTHON, .venv в корне репозитория, .venv основного checkout, python3 из PATH.
 */
export function resolvePython() {
  const explicit = process.env.AGENT_BOARD_PYTHON;
  if (explicit) {
    if (isExecutable(explicit)) return explicit;
    throw new Error(`AGENT_BOARD_PYTHON указывает на ${explicit}, но это не исполняемый файл`);
  }
  const candidates = [venvPython(repo)];
  const main = mainCheckout();
  if (main && main !== repo) candidates.push(venvPython(main));
  for (const c of candidates) if (isExecutable(c)) return c;
  const system = fromPath('python3');
  if (system) return system;
  throw new Error('Не найден Python для UI-тестов: задайте AGENT_BOARD_PYTHON, создайте .venv в корне репозитория '
    + `(${candidates.join(', ')}) или добавьте python3 в PATH`);
}

export const python = resolvePython();
